#!/usr/bin/env python3
"""Re-voice: an Omni clip re-spoken in the user's ElevenLabs voice through ElevenLabs speech-to-speech, room-matched to the video's one room and checked.

Usage:
    python3 scripts/revoice.py --self-test

Imported by run.py.

revoice_clip()  pulls the clip's audio, sends it to ElevenLabs speech-to-speech with the voice and its
                one profile, room-matches the result to the video's room, checks it says the line
                word for word, and swaps it into the video. Up to three tries, then the try that
                failed the fewest checks is kept and flagged.

The call: POST https://api.elevenlabs.io/v1/speech-to-speech/{voice_id}?output_format=mp3_44100_128,
header xi-api-key, multipart/form-data with audio (mono WAV), model_id eleven_multilingual_sts_v2,
voice_settings (a JSON string of stability, similarity_boost and style) and remove_background_noise
false, so the room stays in.

ELEVENLABS_API_KEY is read from ~/.avatar-studio/keys.env, else the environment, at the first call; it
is never printed, logged or written, and is scrubbed from any error text. A rejected key, a used-up
quota or a plan that cannot make the call raises engine.Refused; the caller stops.
--self-test builds a request, reads the refusals and checks the mux on media ffmpeg makes, and makes
no network call. Exits 0 on pass, 1 on fail.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402
import roommatch  # noqa: E402

STS_URL = "https://api.elevenlabs.io/v1/speech-to-speech/%s?output_format=%s"
STS_MODEL = "eleven_multilingual_sts_v2"
OUTPUT_FORMAT = "mp3_44100_128"
TRIES = 3


def _key():
    k = engine.key("ELEVENLABS_API_KEY")
    if not k:
        raise engine.Refused("ElevenLabs: key missing: set ELEVENLABS_API_KEY in %s" % engine.keys_file())
    return k


def refusal(code, body):
    """The ElevenLabs refusal an HTTP error is, or None when it is an ordinary failure."""
    b = (body or "").lower()
    if "quota_exceeded" in b or "insufficient credits" in b or "not enough credits" in b:
        return "quota reached"
    if code == 402 or "paid_plan_required" in b or "payment_required" in b or ("subscription" in b and code == 403):
        return "this plan cannot make this call"
    if "missing_permissions" in b:
        return "key rejected: it lacks a permission this call needs"
    if code == 401 or "invalid_api_key" in b:
        return "key rejected"
    return None


def _post(url, body, content_type, timeout=240):
    k = _key()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"xi-api-key": k, "Content-Type": content_type,
                                          "Accept": "audio/mpeg"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), None
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        why = refusal(e.code, text)
        if why:
            raise engine.Refused("ElevenLabs: " + why)
        return None, "HTTP %d: %s" % (e.code, engine.scrub(text[:300].replace(k, "<key>")))
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, engine.scrub(str(e)[:200].replace(k, "<key>")))


def multipart(fields, file_field, file_path, boundary=None):
    boundary = boundary or uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: audio/wav\r\n\r\n"
             % (boundary, file_field, Path(file_path).name)).encode() + Path(file_path).read_bytes() + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode()
    return body, "multipart/form-data; boundary=%s" % boundary


def sts_url(voice):
    return STS_URL % (urllib.parse.quote(voice["voice_id"], safe=""), OUTPUT_FORMAT)


def voice_fields(voice):
    """The speech-to-speech fields: the model, the voice's one profile, the room kept in. Nothing else."""
    settings = {k: float(voice[k]) for k in ("stability", "similarity_boost", "style")}
    return {"model_id": STS_MODEL, "voice_settings": json.dumps(settings),
            "remove_background_noise": "false"}


def voice_change(wav, voice, out):
    body, ctype = multipart(voice_fields(voice), "audio", wav)
    audio, err = _post(sts_url(voice), body, ctype)
    if err:
        return err
    if not audio:
        return "no audio returned"
    Path(out).write_bytes(audio)
    return None


def heard(path, line, mime_type="audio/mpeg"):
    """(passed, note, errored) for the words in an audio file, a check error run again on the same file."""
    def once():
        text, err = engine.transcribe(path, "audio", mime_type)
        if err:
            return False, "transcribe errored: %s" % err[:120], True
        return engine.words_match(line, text)
    return engine.run_check(once)


def mux(video, audio, out):
    dur = engine.probe(video)[0]
    engine.ff(["ffmpeg", "-loglevel", "error", "-y", "-i", str(video), "-i", str(audio), "-map", "0:v", "-map", "1:a",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", "%.3f" % dur, str(out)])


def revoice_clip(src_mp4, line, voice, out_mp4, work, room_wav):
    """Re-voice one Omni clip into out_mp4, in the room of room_wav. Returns the take's record; raises
    engine.Refused."""
    work = Path(work)
    stem = Path(out_mp4).stem
    src_wav = engine.to_wav(src_mp4, work / ("_%s_src.wav" % stem))
    best, tries = None, []
    try:
        for k in range(1, TRIES + 1):
            dry, roomed = work / ("_%s_rv%d.mp3" % (stem, k)), work / ("_%s_rv%d.wav" % (stem, k))
            err = voice_change(src_wav, voice, dry)
            if err:
                tries.append({"try": k, "checks": {"re-voice": "FAIL - " + err}})
                if err.startswith("HTTP 4"):
                    break  # a request fault: sending it again unchanged cannot fix it
                continue
            roommatch.match(room_wav, dry, roomed)
            checks = {"words": heard(dry, line)}
            tries.append({"try": k, "checks": engine.summarize(checks)})
            n_failed = len(engine.failed(checks))
            if best is None or n_failed < best[0]:
                best = (n_failed, roomed, checks)
            if n_failed == 0:
                break
        rec = {"tries": tries}
        if best is None:
            rec.update(status="flagged", flag=["re-voice"])
        else:
            Path(out_mp4).parent.mkdir(parents=True, exist_ok=True)
            mux(src_mp4, best[1], out_mp4)
            rec.update(status="passed" if best[0] == 0 else "flagged", flag=engine.failed(best[2]))
        return rec
    finally:
        for f in work.glob("_%s_rv*" % stem):
            f.unlink(missing_ok=True)
        src_wav.unlink(missing_ok=True)


def self_test():
    import tempfile
    fails = []
    voice = {"voice_id": "VOICE/1", "stability": 0.5, "similarity_boost": 0.75, "style": 0.3}
    f = voice_fields(voice)
    if sorted(f) != ["model_id", "remove_background_noise", "voice_settings"] \
            or f["remove_background_noise"] != "false" or f["model_id"] != "eleven_multilingual_sts_v2":
        fails.append("the speech-to-speech fields are not the call's three")
    if json.loads(f["voice_settings"]) != {"stability": 0.5, "similarity_boost": 0.75, "style": 0.3}:
        fails.append("voice_settings does not carry the profile as JSON")
    if sts_url(voice) != "https://api.elevenlabs.io/v1/speech-to-speech/VOICE%2F1?output_format=mp3_44100_128":
        fails.append("the speech-to-speech URL is wrong: %s" % sts_url(voice))
    cases = [(401, '{"detail":{"status":"invalid_api_key","message":"Invalid API key"}}', "key rejected"),
             (401, '{"detail":{"status":"quota_exceeded","message":"..."}}', "quota reached"),
             (402, '{"detail":{"status":"payment_required"}}', "this plan cannot make this call"),
             (400, '{"detail":{"status":"voice_not_found"}}', None),
             (422, '{"detail":[{"msg":"bad"}]}', None),
             (500, "internal", None)]
    for code, body, want in cases:
        if refusal(code, body) != want:
            fails.append("refusal(%d) reads %r, not %r" % (code, refusal(code, body), want))
    saved = {n: os.environ.pop(n, None) for n in ("ELEVENLABS_API_KEY", "AVATAR_STUDIO_HOME")}
    try:
        with tempfile.TemporaryDirectory() as d:
            os.environ["AVATAR_STUDIO_HOME"] = d
            try:
                _key()
                fails.append("_key() returned with no ELEVENLABS_API_KEY")
            except engine.Refused as e:
                if "ELEVENLABS_API_KEY" not in str(e):
                    fails.append("the missing-key refusal does not name the setting")
    finally:
        for n, v in saved.items():
            os.environ.pop(n, None)
            if v is not None:
                os.environ[n] = v
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        a = d / "a.wav"
        engine.ff(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=250:duration=4", "-af",
                   "adelay=500,apad=whole_dur=6", "-ar", "44100", "-ac", "1", str(a)])
        body, ctype = multipart(voice_fields(voice), "audio", a, boundary="B")
        if not ctype.endswith("boundary=B") or b'name="audio"; filename="a.wav"' not in body \
                or b'name="model_id"\r\n\r\neleven_multilingual_sts_v2' not in body \
                or b'name="remove_background_noise"\r\n\r\nfalse' not in body:
            fails.append("the multipart body is malformed")
        vid, out = d / "v.mp4", d / "o.mp4"
        engine.ff(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=720x1280:d=6:r=24",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", str(vid)])
        mux(vid, a, out)
        dur, size, kinds = engine.probe(out)
        if size != (720, 1280) or "audio" not in kinds or abs(dur - 6.0) > 0.1:
            fails.append("mux() did not make a 720x1280 clip with sound at the video's length")
    for msg in fails:
        print("revoice.py self-test: %s" % msg)
    print("revoice.py self-test: %s" % ("pass" if not fails else "fail"))
    return 0 if not fails else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(self_test())
    print(__doc__)
    sys.exit(0 if sys.argv[1:2] in (["-h"], ["--help"]) else 2)
