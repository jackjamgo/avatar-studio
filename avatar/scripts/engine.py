#!/usr/bin/env python3
"""Avatar Studio engine: one line in, one checked 9:16 talking-avatar clip out, with up to three tries and every clip check.

Usage:
    python3 scripts/engine.py --self-test

Imported by run.py, revoice.py, roommatch.py, library.py and setup.py. Holds, each in one place: the
model names, MAX_TRIES, the pace, PRICE_PER_SEC, the prompt, the Gemini call, the refusal reader, the
four clip checks (file, cuts, words, look), the image check, the key reader, and the ffmpeg binary.

ffmpeg is the one imageio-ffmpeg carries (python3 -m pip install --user imageio-ffmpeg), else an ffmpeg
on PATH. Every ffmpeg call in the skill goes through ff(); media facts come from probe(), which reads
ffmpeg's own report of the file, so no ffprobe is needed.

The keys live in ~/.avatar-studio/keys.env as KEY=VALUE lines (blank lines, # comments and placeholder
values are ignored), with the environment as the fallback. Set AVATAR_STUDIO_HOME to use another folder.
A key is read at the first call, never at import, and is never printed, logged or written anywhere;
error text has every key value scrubbed out. A spend-cap, quota or key refusal raises Refused; the
caller stops. A check that errors is run again on the same file, up to two more times, and a check
error never triggers a paid render.

--self-test runs the offline parts (the prompt, the word compare, the refusal reader, the key reader,
and the file, cuts and image checks on media ffmpeg makes) with no network call. Exits 0 on pass,
1 on fail.
"""

import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True

API = "https://generativelanguage.googleapis.com/v1beta/interactions"
OMNI = "gemini-omni-1.1-flash"
CHECKER = "gemini-3.8-flash"
MAX_TRIES = 3
CHECK_RUNS = 3          # a check that errors runs again on the same file, up to two more times
WORDS_PER_SEC = 3.3
PRICE_PER_SEC = 0.10    # Omni, US dollars per second of clip billed
SHORT_SPEAK_S = 4       # a speaking window this short asks for SHORT_CLIP_S, else LONG_CLIP_S
SHORT_CLIP_S = 5
LONG_CLIP_S = 10
WIDTH, HEIGHT = 720, 1280
KEY_NAMES = ("GEMINI_API_KEY", "ELEVENLABS_API_KEY")
INSTALL_LINE = "python3 -m pip install --user imageio-ffmpeg"


class Refused(Exception):
    """A service refused: spend cap reached, quota reached, key rejected or key missing."""


# ---------------------------------------------------------------- the keys

def home():
    """The Avatar Studio folder: AVATAR_STUDIO_HOME, else ~/.avatar-studio."""
    return Path(os.environ.get("AVATAR_STUDIO_HOME") or (Path.home() / ".avatar-studio"))


def keys_file():
    return home() / "keys.env"


def placeholder(value):
    """True for an empty value or one setup.py's template leaves in place."""
    v = (value or "").strip().lower()
    return (not v or v.startswith(("paste", "your", "<", "xxx", "replace", "put-")) or
            v in ("changeme", "none", "null", "todo"))


def read_keys_file(path=None):
    """{name: value} for every real KEY=VALUE line in keys.env; blanks, comments and placeholders skipped."""
    path = Path(path) if path else keys_file()
    out = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return out
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        if ln.startswith("export "):
            ln = ln[7:].strip()
        name, value = ln.split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        if name and not placeholder(value):
            out[name] = value
    return out


def key_source(name):
    """'keys.env', 'environment' or None: where a key would be read from. Never the value."""
    if name in read_keys_file():
        return "keys.env"
    if not placeholder(os.environ.get(name, "")):
        return "environment"
    return None


def key(name):
    """The key's value from keys.env, else the environment, else ''. The caller never prints it."""
    v = read_keys_file().get(name)
    if v:
        return v
    v = os.environ.get(name, "").strip()
    return "" if placeholder(v) else v


def scrub(text):
    """The text with every known key value replaced by <key>."""
    text = str(text or "")
    for name in KEY_NAMES:
        v = key(name)
        if v and len(v) >= 6:
            text = text.replace(v, "<key>")
    return text


# ---------------------------------------------------------------- the API

def _key():
    k = key("GEMINI_API_KEY")
    if not k:
        raise Refused("Gemini: key missing: set GEMINI_API_KEY in %s" % keys_file())
    return k


def refusal(code, body):
    """The refusal an HTTP error is, or None when it is an ordinary failure."""
    b = (body or "").lower()
    if code == 429 and "spending cap" in b:
        return "spend cap reached"
    if code == 429 and ("quota" in b or "resource_exhausted" in b) and "rate" not in b:
        return "quota reached"
    if code in (400, 401, 403) and ("api key" in b or "api_key" in b or "permission_denied" in b
                                    or "unauthenticated" in b):
        return "key rejected"
    return None


def call(body, timeout=420, url=API):
    """POST one interaction. Returns (response, None) or (None, error line); raises Refused."""
    k = _key()
    data = json.dumps(body).encode()
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": k})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r), None
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            why = refusal(e.code, text)
            if why:
                raise Refused("Gemini: " + why)
            if e.code == 429 and attempt < 2:  # a rate limit: wait, then the same call again
                time.sleep(20 * (attempt + 1))
                continue
            return None, "HTTP %d: %s" % (e.code, scrub(text[:300].replace(k, "<key>")))
        except Exception as e:  # timeouts, resets
            return None, "%s: %s" % (type(e).__name__, scrub(str(e)[:200].replace(k, "<key>")))
    return None, "rate limited three times"


def http_get(url, headers, timeout=30):
    """One GET. Returns (HTTP status, or None when nothing answered; body text with every key scrubbed)."""
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, scrub(e.read().decode("utf-8", "replace")[:500])
    except Exception as e:  # no network, timeouts, resets
        return None, scrub("%s: %s" % (type(e).__name__, str(e)[:200]))


def media_out(resp, kind):
    for step in resp.get("steps", []) or []:
        if step.get("type") == "model_output":
            for c in step.get("content", []) or []:
                if c.get("type") == kind and c.get("data"):
                    return base64.b64decode(c["data"])
    return None


def text_out(resp):
    return " ".join(c.get("text", "") for s in resp.get("steps", []) or [] if s.get("type") == "model_output"
                    for c in s.get("content", []) or [] if c.get("type") == "text").strip()


def b64(path):
    return base64.b64encode(Path(path).read_bytes()).decode()


def mime(path):
    head = Path(path).read_bytes()[:12]
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


# ---------------------------------------------------------------- timing and the prompt

def timing(words):
    """(speaking window, clip length) in seconds for a line of `words` words."""
    speak_s = max(3, min(9, math.ceil(words / WORDS_PER_SEC)))
    return speak_s, (SHORT_CLIP_S if speak_s <= SHORT_SPEAK_S else LONG_CLIP_S)


def prompt(say, speak_s, clip_s):
    """The one prompt. Neutral: the presenter is whoever the image shows."""
    return (
        "<FIRST_FRAME> A single continuous unbroken shot with a locked-off camera. "
        "The presenter in this image stays exactly as posed and framed in the image and speaks directly "
        "to the camera, vertical 9:16, same room, same clothes, same lighting, same framing.\n"
        "The presenter speaks at a brisk, energetic social-media ad pace, about 3.3 words per second, quick and "
        "punchy, like a confident expert talking to a friend. No long pauses between phrases; commas get only a "
        "tiny breath.\n"
        "[0-%ds] The presenter says, word for word, in a warm, clear voice: \"%s\"\n"
        "[%d-%ds] The presenter finishes, closes their mouth and holds a warm smile, silent and still.\n"
        "Rules: no scene cuts, no cutaways, no b-roll, no product shots, no close-ups of objects, no zoom, "
        "no animations, no on-screen text or captions, no music, no sound effects, no second person. "
        "The presenter's voice is the only sound. Say every word exactly once. Do not add, repeat or change "
        "any words." % (speak_s, say, speak_s, clip_s))


# ---------------------------------------------------------------- media helpers

_FFMPEG = []


def install_line(venv=None, managed=None):
    """The one line that installs ffmpeg for this Python: INSTALL_LINE, without --user inside a virtual
    environment (pip refuses it there), with --break-system-packages on a Python that marks itself
    externally managed (PEP 668, as Homebrew's does)."""
    if venv is None:
        venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if managed is None:
        try:
            managed = (Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").is_file()
        except (KeyError, TypeError, OSError):
            managed = False
    if venv:
        return "python3 -m pip install imageio-ffmpeg"
    if managed:
        return "python3 -m pip install --user --break-system-packages imageio-ffmpeg"
    return INSTALL_LINE


def ffmpeg_exe():
    """The ffmpeg binary: imageio-ffmpeg's own, else an ffmpeg on PATH, else None. Looked up once."""
    if not _FFMPEG:
        exe = None
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # not installed, or its binary is missing
            exe = None
        _FFMPEG.append(exe if exe and os.path.isfile(exe) else shutil.which("ffmpeg"))
    return _FFMPEG[0]


def ff(args, text=True, input=None):
    """Run ffmpeg. args[0] is "ffmpeg" and is swapped for ffmpeg_exe(); raises FileNotFoundError, with the
    install line, when there is none."""
    args = [str(a) for a in args]
    if args and args[0] == "ffmpeg":
        exe = ffmpeg_exe()
        if not exe:
            raise FileNotFoundError("ffmpeg not found: install it with %s" % install_line())
        args[0] = exe
    if text:
        return subprocess.run(args, capture_output=True, text=True, errors="replace", input=input)
    return subprocess.run(args, capture_output=True, input=input)


def _hms(h, m, s):
    return int(h) * 3600 + int(m) * 60 + float(s)


def probe(path):
    """(duration, (width, height) or None, stream kinds) of a media file, read off ffmpeg's own report
    of it. A file whose container gives no duration is decoded once to time it."""
    err = ff(["ffmpeg", "-hide_banner", "-i", str(path)]).stderr or ""
    kinds, size = [], None
    for ln in err.splitlines():
        s = re.match(r"\s*Stream #\d+:\d+[^:]*: (Video|Audio|Subtitle|Data|Attachment): (.*)", ln)
        if not s:
            continue
        kinds.append(s.group(1).lower())
        z = re.search(r"(?:^|[ ,])(\d+)x(\d+)(?=[ ,\[]|$)", s.group(2))
        if s.group(1) == "Video" and size is None and z:
            size = (int(z.group(1)), int(z.group(2)))
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        dur = _hms(*m.groups())
    elif kinds:
        times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)",
                           ff(["ffmpeg", "-hide_banner", "-i", str(path), "-f", "null", "-"]).stderr or "")
        dur = _hms(*times[-1]) if times else 0.0
    else:
        dur = 0.0
    return dur, size, kinds


def to_wav(src, wav, rate=44100):
    ff(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", str(rate), str(wav)])
    return Path(wav)


def silences(wav, noise_db=-35, min_s=0.25):
    """[(start, end)] of every silence; end is None when the silence runs to the end."""
    err = ff(["ffmpeg", "-i", str(wav), "-af", "silencedetect=n=%ddB:d=%s" % (noise_db, min_s),
              "-f", "null", "-"]).stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?[0-9.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", err)]
    return [(s, ends[i] if i < len(ends) else None) for i, s in enumerate(starts)]


def speech_span(wav, dur):
    """(speech start, speech end) read off the silence map."""
    sil = silences(wav)
    start, end = 0.0, dur
    if sil and sil[0][0] <= 0.05 and sil[0][1] is not None:
        start = sil[0][1]
    if sil and (sil[-1][1] is None or sil[-1][1] >= dur - 0.05) and sil[-1][0] > start:
        end = sil[-1][0]
    return start, end


# ---------------------------------------------------------------- the avatar image

def convert_image(src, out_jpg):
    """Convert the avatar image to a JPEG at out_jpg and check it. Returns (size or None, message);
    the image passes when the size is set and the message carries no 'too small or not portrait'."""
    src, out_jpg = Path(src), Path(out_jpg)
    if not src.is_file() or not src.stat().st_size:
        return None, "missing"
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    ff(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-frames:v", "1", "-q:v", "2", str(out_jpg)])
    _, size, _ = probe(out_jpg) if out_jpg.exists() else (0, None, [])
    if not size:
        return None, "missing"
    return size, image_note(size)


def image_ok(size):
    return bool(size) and size[0] >= WIDTH and size[1] > size[0]


def image_note(size):
    w, h = size
    return "%dx%d%s" % (w, h, "" if image_ok(size) else ", too small or not portrait")


# ---------------------------------------------------------------- words

def norm_words(s):
    s = (s or "").lower().replace("’", "'")
    s = re.sub(r"[^a-z0-9'%$. ]+", " ", s)
    s = re.sub(r"(?<![0-9])\.|\.(?![0-9])", " ", s)  # drop sentence dots, keep 99.9
    return [w.strip("'") for w in s.split() if w.strip("'")]


def same_but_written_differently(script, heard):
    """(same, errored): same only when the two differ in how numbers, symbols or spacing are written."""
    resp, err = call({"model": CHECKER, "input": (
        "Script: " + json.dumps(script) + "\nHeard: " + json.dumps(heard) + "\n"
        "Were exactly the same words spoken, in the same order, with nothing added, dropped, repeated or changed? "
        "Treat only these as the same: numbers or symbols written as digits versus words (120 / one hundred twenty, "
        "99.9% / ninety-nine point nine percent, UV-C / U V C), and joined versus split words (buildup / build up). "
        "Answer SAME or DIFFERENT, then one short reason.")}, timeout=120)
    if err:
        return False, True
    return text_out(resp).upper().startswith("SAME"), False


def words_match(line, heard):
    """(passed, note, errored) for a transcript against the written line."""
    a, b = norm_words(line), norm_words(heard)
    if a == b:
        return True, "word for word", False
    same, errored = same_but_written_differently(line, heard)
    if errored:
        return False, "the compare call errored", True
    if same:
        return True, "word for word, numbers or symbols written differently", False
    i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
    return False, "words differ at word %d: script '%s', heard '%s'" % (
        i + 1, " ".join(a[i:i + 4]), " ".join(b[i:i + 4])), False


def transcribe(path, kind, mime_type):
    resp, err = call({"model": CHECKER, "input": [
        {"type": kind, "mime_type": mime_type, "data": b64(path)},
        {"type": "text", "text": "Transcribe exactly the words spoken, verbatim, including any repeated or extra "
                                 "words. Output only the transcript."}]}, timeout=180)
    if err:
        return None, err
    return text_out(resp), None


# ---------------------------------------------------------------- the four checks
# Each returns (passed, note, errored). errored means the check itself did not run.

def check_file(mp4, clip_s=LONG_CLIP_S):
    dur, size, kinds = probe(mp4)
    ok = size == (WIDTH, HEIGHT) and "audio" in kinds and 4.5 <= dur <= clip_s + 0.5
    return ok, "%s, %.2fs, streams %s" % (size, dur, kinds), False


def check_cuts(mp4):
    err = ff(["ffmpeg", "-i", str(mp4), "-vf", "select='gt(scene,0.3)',showinfo", "-f", "null", "-"]).stderr
    cuts = [ln.split("pts_time:")[1].split()[0] for ln in err.splitlines() if "pts_time:" in ln]
    return not cuts, ("no cuts" if not cuts else "cuts at %s" % ", ".join(cuts)), False


def check_words(mp4, line):
    heard, err = transcribe(mp4, "video", "video/mp4")
    if err:
        return False, "transcribe errored: %s" % err[:120], True
    ok, note, errored = words_match(line, heard)
    return ok, note, errored


def check_look(mp4, image, dur, workdir):
    frames = []
    stem = Path(mp4).stem  # per clip, so clips checked side by side never share a frame file
    for k, t in enumerate([0.5, dur / 2, max(0.6, dur - 0.5)]):
        f = Path(workdir) / ("_%s_frame%d.jpg" % (stem, k))
        ff(["ffmpeg", "-loglevel", "error", "-y", "-ss", "%.2f" % t, "-i", str(mp4), "-frames:v", "1",
            "-vf", "scale=360:-1", str(f)])
        frames.append(f)
    inp = [{"type": "image", "mime_type": mime(image), "data": b64(image)}] + \
          [{"type": "image", "mime_type": "image/jpeg", "data": b64(f)} for f in frames if f.exists()] + \
          [{"type": "text", "text": "Image 1 is the reference image of a presenter. The other images are frames from "
                                    "a video. PASS only if every frame shows the same person as the reference, "
                                    "talking to camera, in the same room and clothes, with no other shot, no product "
                                    "or object close-up, no extra people and no on-screen text or captions. Answer "
                                    "PASS or FAIL, then one short reason."}]
    try:
        resp, err = call({"model": CHECKER, "input": inp}, timeout=180)
    finally:
        for f in frames:
            f.unlink(missing_ok=True)
    if err:
        return False, "look check errored: %s" % err[:120], True
    ans = text_out(resp)
    return ans.upper().startswith("PASS"), ans.replace("\n", " ")[:160], False


def run_check(fn, *args):
    """A check run up to CHECK_RUNS times while it errors. Returns (passed, note, errored)."""
    res = (False, "not run", True)
    for _ in range(CHECK_RUNS):
        res = fn(*args)
        if not res[2]:
            return res
    return res


def check_clip(mp4, line, image, clip_s, workdir):
    """Every check on one Omni clip. Returns {check: (passed, note, errored)}."""
    workdir = Path(workdir)
    dur, _, _ = probe(mp4)
    checks = {"file": check_file(mp4, clip_s), "cuts": check_cuts(mp4)}
    checks["words"] = run_check(check_words, mp4, line)
    checks["look"] = run_check(check_look, mp4, image, dur, workdir)
    return checks


def summarize(checks):
    return {k: ("pass" if v[0] else ("ERROR" if v[2] else "FAIL")) + " - " + v[1] for k, v in checks.items()}


def failed(checks):
    return [k for k, v in checks.items() if not v[0]]


def only_errored(checks):
    bad = [v for v in checks.values() if not v[0]]
    return bool(bad) and all(v[2] for v in bad)


# ---------------------------------------------------------------- one render

def render(say, image, clip_s, out_mp4):
    """One Omni render. Returns (seconds billed, error line or None); raises Refused."""
    speak_s = min(clip_s - 1, timing(len(say.split()))[0])
    resp, err = call({"model": OMNI, "store": False,
                      "input": [{"type": "image", "mime_type": mime(image), "data": b64(image)},
                                {"type": "text", "text": prompt(say, speak_s, clip_s)}],
                      "response_format": {"type": "video", "aspect_ratio": "9:16", "duration": "%ds" % clip_s}})
    video = media_out(resp, "video") if resp else None
    if not video:
        return 0.0, "render failed: %s" % (err or "no video returned")
    Path(out_mp4).write_bytes(video)
    return probe(out_mp4)[0], None


# ---------------------------------------------------------------- self-test

def self_test():
    import tempfile
    fails = []
    p = prompt("Say this line.", 4, 5)
    if re.search(r"\b(he|she|his|her|him|woman|man)\b", p, re.I):
        fails.append("the prompt is not neutral")
    if "\"Say this line.\"" not in p or "[0-4s]" not in p or "[4-5s]" not in p:
        fails.append("the prompt does not carry the line and its timed window")
    if timing(13) != (4, 5) or timing(14) != (5, 10) or timing(30) != (9, 10) or timing(3) != (3, 5):
        fails.append("timing() gives the wrong windows")
    if norm_words("It's 99.9% clean. Done!") != ["it's", "99.9%", "clean", "done"]:
        fails.append("norm_words() does not keep 99.9% and drop sentence dots")
    if words_match("You can't quit the plastic.", "you can't quit the plastic")[0] is not True:
        fails.append("words_match() fails an equal transcript")
    cap = '{"error":{"message":"Your project has exceeded its monthly spending cap.","code":"too_many_requests"}}'
    cases = [(429, cap, "spend cap reached"),
             (429, '{"error":{"status":"RESOURCE_EXHAUSTED","message":"Quota exceeded"}}', "quota reached"),
             (400, '{"error":{"message":"API key not valid.","status":"INVALID_ARGUMENT"}}', "key rejected"),
             (403, '{"error":{"status":"PERMISSION_DENIED"}}', "key rejected"),
             (429, '{"error":{"message":"rate limit, retry later"}}', None),
             (500, "internal", None)]
    for code, body, want in cases:
        if refusal(code, body) != want:
            fails.append("refusal(%d) reads %r, not %r" % (code, refusal(code, body), want))
    saved_env = {n: os.environ.pop(n, None) for n in KEY_NAMES + ("AVATAR_STUDIO_HOME",)}
    try:
        with tempfile.TemporaryDirectory() as d:
            os.environ["AVATAR_STUDIO_HOME"] = d
            try:
                call({"model": CHECKER, "input": "x"})
                fails.append("call() ran with no key")
            except Refused as e:
                if "GEMINI_API_KEY" not in str(e):
                    fails.append("the missing-key refusal does not name the setting")
            Path(d, "keys.env").write_text(
                "# a comment\n\nGEMINI_API_KEY=paste-your-gemini-key-here\n"
                "export ELEVENLABS_API_KEY=\"sk_test_value_123\"\nOTHER = plain # trailing note\n")
            got = read_keys_file()
            if got != {"ELEVENLABS_API_KEY": "sk_test_value_123", "OTHER": "plain"}:
                fails.append("read_keys_file() reads %s" % sorted(got))
            if key("GEMINI_API_KEY") or key_source("GEMINI_API_KEY") is not None:
                fails.append("a placeholder value is read as a key")
            os.environ["GEMINI_API_KEY"] = "env_value_456789"
            if key("GEMINI_API_KEY") != "env_value_456789" or key_source("GEMINI_API_KEY") != "environment":
                fails.append("the environment is not the fallback")
            if key_source("ELEVENLABS_API_KEY") != "keys.env":
                fails.append("keys.env is not read first")
            if scrub("x sk_test_value_123 y env_value_456789") != "x <key> y <key>":
                fails.append("scrub() leaves a key in the text")
    finally:
        for n, v in saved_env.items():
            os.environ.pop(n, None)
            if v is not None:
                os.environ[n] = v
    with tempfile.TemporaryDirectory() as d:
        good = os.path.join(d, "good.mp4")
        ff(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=720x1280:d=6:r=24",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=4", "-af", "apad=whole_dur=6",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", good])
        cut = os.path.join(d, "cut.mp4")
        ff(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=720x1280:d=3:r=24",
            "-f", "lavfi", "-i", "color=c=white:s=720x1280:d=3:r=24",
            "-filter_complex", "[0][1]concat=n=2:v=1[v]", "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", cut])
        if not os.path.exists(good) or not os.path.exists(cut):
            fails.append("ffmpeg could not make the test clips")
        else:
            if not check_file(good, 10)[0]:
                fails.append("check_file fails a good 720x1280 clip: %s" % check_file(good, 10)[1])
            if check_file(cut, 10)[0]:
                fails.append("check_file passes a clip with no sound")
            if not check_cuts(good)[0]:
                fails.append("check_cuts finds a cut in one still shot")
            if check_cuts(cut)[0]:
                fails.append("check_cuts misses a hard cut")
            wav = to_wav(good, os.path.join(d, "good.wav"))
            dur = probe(good)[0]
            start, end = speech_span(wav, dur)
            if abs(start) > 0.1 or abs(end - 4.0) > 0.2:
                fails.append("speech_span reads %.2f to %.2f, not 0 to 4" % (start, end))
            if mime(good) != "image/jpeg":  # an unknown head reads as JPEG
                fails.append("mime() does not fall back to JPEG")
            if abs(dur - 6.0) > 0.05 or probe(wav)[1] is not None or probe(wav)[2] != ["audio"] \
                    or abs(probe(wav)[0] - 6.0) > 0.05:
                fails.append("probe() reads the clip as %s and its sound as %s" % (probe(good), probe(wav)))
            if probe(cut)[1:] != ((720, 1280), ["video"]):
                fails.append("probe() reads a clip with no sound as %s" % (probe(cut),))
        tall, wide = os.path.join(d, "t.png"), os.path.join(d, "w.png")
        ff(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=768x1376", "-frames:v", "1", tall])
        ff(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=1280x720", "-frames:v", "1", wide])
        size, msg = convert_image(tall, os.path.join(d, "out", "a.jpg"))
        if size != (768, 1376) or "too small" in msg or mime(os.path.join(d, "out", "a.jpg")) != "image/jpeg":
            fails.append("convert_image() does not convert a portrait image: %s" % msg)
        if "not portrait" not in convert_image(wide, os.path.join(d, "b.jpg"))[1]:
            fails.append("convert_image() passes a landscape image")
        if convert_image(os.path.join(d, "none.png"), os.path.join(d, "c.jpg"))[0] is not None:
            fails.append("convert_image() passes a missing file")
        if probe(os.path.join(d, "none.png")) != (0.0, None, []):
            fails.append("probe() reads a missing file as media")
    if not ffmpeg_exe():
        fails.append("no ffmpeg found: install it with %s" % install_line())
    saved_exe = list(_FFMPEG)
    _FFMPEG[:] = [None]
    try:
        ff(["ffmpeg", "-version"])
        fails.append("ff() ran with no ffmpeg")
    except FileNotFoundError as e:
        if install_line() not in str(e):
            fails.append("the no-ffmpeg error does not give the install line")
    finally:
        _FFMPEG[:] = saved_exe
    for f in fails:
        print("engine.py self-test: %s" % f)
    print("engine.py self-test: %s" % ("pass" if not fails else "fail"))
    return 0 if not fails else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(self_test())
    print(__doc__)
    sys.exit(0 if sys.argv[1:2] in (["-h"], ["--help"]) else 2)
