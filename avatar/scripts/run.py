#!/usr/bin/env python3
"""Run one talking-avatar video: cut a pasted script into clips and price it, render each clip on Gemini Omni from one avatar image with every check, voice every take (re-voiced in the user's ElevenLabs voice in the video's one room, or Omni's own voice kept), then join the takes into one video.

Usage:
    python3 scripts/run.py preflight
    python3 scripts/run.py image DIR FILE
    python3 scripts/run.py plan DIR (--script FILE | --test-line [TEXT]) --voice VOICE [--tries N] [--respell JSON]
    python3 scripts/run.py render DIR [--only CLIP ...] [--max-usd N] [--tries N]
    python3 scripts/run.py recheck DIR [--only CLIP ...] [--tries N]
    python3 scripts/run.py stitch DIR
    python3 scripts/run.py --self-test

DIR is the video's folder. Every working file sits in DIR/_work/; the final takes land in DIR/clips/
as clip_01.mp4, clip_02.mp4 ...; the joined video is DIR/final.mp4. Nothing is written anywhere else.

preflight  python3 3.9+ and ffmpeg (imageio-ffmpeg's, else one on PATH) are here, and GEMINI_API_KEY
           is set; ELEVENLABS_API_KEY is reported as set or not (it is needed only for an ElevenLabs
           voice). Prints names only, never a value.
image      FILE, the avatar image, converted to DIR/_work/avatar.jpg; prints its size. Exit 1 when it is
           missing, under 720 wide, or not taller than wide.
plan       reads the pasted script from FILE, cuts each paragraph into clips of at most 30 words (at a
           sentence end, then a colon or semicolon, then a comma, never changing a word), names them
           clip_01, clip_02 ..., and prices the video. VOICE is `native` (keep Omni's own voice) or a
           JSON file or string: {"voice_id", "stability", "similarity_boost", "style"}, the profile
           defaulting to 0.5 / 0.75 / 0.3; `library.py show NAME` prints one. --respell is a JSON file or
           string {written: said}, applied to the line Omni is told to say only. Writes
           DIR/_work/plan.json and prints the numbered clip table, the clip count, the first-try estimate
           and the worst case at --tries N. No network. --test-line plans one line as one clip named
           test_line in place of --script; with no TEXT it plans the standard test line.
render     renders what has not yet passed, three clips at a time, up to three tries a clip, or the N
           --tries gives, from 1 to 3; a clip that fails its last try is kept, the try that failed the
           fewest checks, and flagged. Then, with an ElevenLabs voice, saves the video's room,
           DIR/_work/room.wav, once, from the first clip with an Omni original, a passed one first,
           and re-voices every clip in it; with `native`, keeps each clip's own audio.
           --only renders only those clips (a clip name or its number), each from a fresh first try.
           --max-usd stops before any render that would take this run's Omni spend past N.
recheck    re-runs the checks on the Omni files already made, voices any clip that now passes and has
           no final take, and renders nothing. --tries gives the tries the render ran at, so a clip
           that spent them settles as flagged.
stitch     joins every final take in plan order into DIR/final.mp4, re-encoded to 720x1280 at 30 fps
           with 48 kHz stereo AAC. Exit 2 with one line when a take is missing.

DIR/_work/report.json holds each take's tries, each check, billed seconds and cost; no key.
Exit: 0 every take passed; 1 some takes flagged; 2 bad input; 3 a service refused (the Gemini spend cap,
quota or key, or the ElevenLabs key, quota or plan), the files made so far kept; 5 stopped at --max-usd.
"""

import argparse
import json
import os
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402

CAP_WORDS = 30
WORKERS = 3
JOINERS = {"but", "and", "so", "or"}
DEFAULT_PROFILE = {"stability": 0.5, "similarity_boost": 0.75, "style": 0.3}
TEST_LINE = ("This is a test render for Avatar Studio. If you can hear this clearly, everything is working.")
STITCH_FPS = 30


class BadInput(Exception):
    pass


class OverBudget(Exception):
    pass


# ---------------------------------------------------------------- the splitter and the namer

def sentences(text):
    return [s for s in re.split(r"(?<=[.?!])\s+", text.strip()) if s]


def _joiner(word):
    return word.strip("\"'“‘(").lower().rstrip(",.;:") in JOINERS


def units(text, cap=CAP_WORDS):
    """Whole sentences; one over `cap` words splits at the colon or semicolon nearest its middle, then a comma,
    never leaving a piece that opens on a joining word where another break exists."""
    out = []
    for s in sentences(text):
        w = s.split()
        if len(w) <= cap:
            out.append(s)
            continue
        cuts = []
        for marks in ((":", ";"), (",",)):
            cuts = [i + 1 for i, x in enumerate(w[:-1]) if x.endswith(marks)]
            clean = [c for c in cuts if not _joiner(w[c])]
            cuts = clean or cuts
            if clean:
                break
        if not cuts:
            mid = len(w) // 2
            cuts = sorted(range(1, len(w)), key=lambda c: (_joiner(w[c]), abs(c - mid)))[:1]
        i = min(cuts, key=lambda c: (_joiner(w[c]), abs(c - len(w) / 2)))
        out += units(" ".join(w[:i]), cap) + units(" ".join(w[i:]), cap)
    return out


def pack(text, cap=CAP_WORDS):
    clips, cur = [], []
    for u in units(text, cap):
        if cur and len(" ".join(cur + [u]).split()) > cap:
            clips.append(" ".join(cur))
            cur = []
        cur.append(u)
    return clips + ([" ".join(cur)] if cur else [])


def paragraphs(script):
    """The script's paragraphs, split at blank lines, each joined onto one line. A clip never spans two."""
    text = (script or "").replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")
    return [" ".join(p.split()) for p in re.split(r"\n\s*\n", text) if p.split()]


def clip_names(n):
    width = max(2, len(str(n)))
    return ["clip_%0*d" % (width, k) for k in range(1, n + 1)]


def read_voice(value):
    """The voice from --voice: 'native', or a JSON file or string with a voice_id and an optional profile."""
    if value is None:
        raise BadInput("--voice is required: native, or a voice JSON (library.py show NAME prints one)")
    if value.strip().lower() == "native":
        return {"mode": "native"}
    try:
        v = json.loads(Path(value).read_text() if os.path.isfile(value) else value)
    except (ValueError, OSError) as e:
        raise BadInput("--voice is neither native nor JSON: %s" % e)
    if v == "native" or (isinstance(v, dict) and v.get("mode") == "native"):
        return {"mode": "native"}
    if not isinstance(v, dict) or not str(v.get("voice_id") or "").strip():
        raise BadInput("--voice has no voice_id")
    out = {"mode": "elevenlabs", "voice_id": str(v["voice_id"]).strip()}
    if v.get("name"):
        out["name"] = str(v["name"])
    for k, default in DEFAULT_PROFILE.items():
        x = v.get(k, default)
        try:
            x = float(x)
        except (TypeError, ValueError):
            raise BadInput("the voice's %s is not a number" % k)
        if not 0 <= x <= 1:
            raise BadInput("the voice's %s is %s, not between 0 and 1" % (k, x))
        out[k] = x
    return out


def read_respell(value):
    if not value:
        return {}
    try:
        r = json.loads(Path(value).read_text() if os.path.isfile(value) else value)
    except (ValueError, OSError) as e:
        raise BadInput("--respell is not JSON: %s" % e)
    if not isinstance(r, dict) or not all(isinstance(k, str) and isinstance(x, str) for k, x in r.items()):
        raise BadInput("--respell is not a {written: said} table")
    return r


def make_plan(script, voice, tries=engine.MAX_TRIES, respell=None, names=None, source=None):
    respell = respell or {}
    lines = []
    for k, para in enumerate(paragraphs(script), 1):
        pieces = pack(para)
        if " ".join(pieces).split() != para.split():
            raise BadInput("cutting paragraph %d changed its words" % k)
        lines += [(k, x) for x in pieces]
    if not lines:
        raise BadInput("the script has no words")
    clips = []
    for name, (para, line) in zip(names or clip_names(len(lines)), lines):
        speak_s, clip_s = engine.timing(len(line.split()))
        say = line
        for written, said in respell.items():
            say = say.replace(written, said)
        clips.append({"clip": name, "paragraph": para, "line": line, "say": say,
                      "words": len(line.split()), "speak_s": speak_s, "clip_s": clip_s})
    secs = sum(c["clip_s"] for c in clips)
    return {"source": source, "voice": voice, "respell": respell, "clips": clips,
            "clip_seconds": secs, "estimate_usd": round(secs * engine.PRICE_PER_SEC, 2),
            "worst_usd": round(secs * engine.PRICE_PER_SEC * tries, 2), "tries": tries,
            "price_per_sec": engine.PRICE_PER_SEC}


def print_plan(plan):
    v = plan["voice"]
    if v["mode"] == "native":
        print("voice: native (Omni's own voice, no re-voice)")
    else:
        print("voice: ElevenLabs %s (stability %s, similarity %s, style %s)" % (
            ("%s, %s" % (v["name"], v["voice_id"])) if v.get("name") else v["voice_id"],
            v["stability"], v["similarity_boost"], v["style"]))
    w = len(str(len(plan["clips"])))
    print("%*s  words  secs  line" % (w, "#"))
    for k, c in enumerate(plan["clips"], 1):
        print("%*d  %5d  %4d  %s" % (w, k, c["words"], c["clip_s"], c["line"]))
        if c["say"] != c["line"]:
            print("%*s  %5s  %4s  said as: %s" % (w, "", "", "", c["say"]))
    print("clips: %d; %ds of video; estimate: $%.2f first try; up to $%.2f at %d %s" % (
        len(plan["clips"]), plan["clip_seconds"], plan["estimate_usd"], plan["worst_usd"], plan["tries"],
        "try" if plan["tries"] == 1 else "tries"))
    if v["mode"] == "elevenlabs":
        print("ElevenLabs re-voice: about %.1f minutes of audio a pass, billed on your ElevenLabs plan" % (
            plan["clip_seconds"] / 60.0))


# ---------------------------------------------------------------- the commands

def preflight():
    """(missing names, notes). Names only, never a value."""
    missing, notes = [], []
    if sys.version_info < (3, 9):
        missing.append("python3 3.9 or newer (this is %d.%d)" % sys.version_info[:2])
    if engine.ffmpeg_exe():
        notes.append("ffmpeg is here (%s)" % engine.ffmpeg_exe())
    else:
        missing.append("ffmpeg (install it with: %s)" % engine.install_line())
    src = engine.key_source("GEMINI_API_KEY")
    if src:
        notes.append("GEMINI_API_KEY is set (%s)" % src)
    else:
        missing.append("GEMINI_API_KEY")
    src = engine.key_source("ELEVENLABS_API_KEY")
    notes.append("ELEVENLABS_API_KEY is set (%s)" % src if src else
                 "ELEVENLABS_API_KEY is not set: only the native voice can run")
    return missing, notes


def image(d, src):
    return engine.convert_image(src, Path(d) / "_work" / "avatar.jpg")


class Run:
    def __init__(self, d, max_usd=None, tries=engine.MAX_TRIES):
        self.d = Path(d)
        self.work = self.d / "_work"
        self.clips_dir = self.d / "clips"
        self.plan = json.loads((self.work / "plan.json").read_text())
        self.voice = self.plan["voice"]
        self.avatar = self.work / "avatar.jpg"
        rp = self.work / "report.json"
        self.report = json.loads(rp.read_text()) if rp.exists() else {"clips": {}}
        self.lock = threading.Lock()
        self.max_usd = max_usd
        self.tries = tries
        self.spent = 0.0
        self.stop = threading.Event()
        self.refused = None
        self.over = False

    def final(self, name):
        return self.clips_dir / ("%s.mp4" % name)

    def save(self):
        with self.lock:
            clips = self.report["clips"].values()
            billed = round(sum(c.get("billed_s", 0) for c in clips), 1)
            self.report.update(voice=self.voice["mode"], price_per_sec=engine.PRICE_PER_SEC,
                               estimate_usd=self.plan["estimate_usd"], billed_s=billed,
                               cost_usd=round(billed * engine.PRICE_PER_SEC, 2))
            (self.work / "report.json").write_text(json.dumps(self.report, indent=1, ensure_ascii=False))

    def budget(self, clip_s):
        with self.lock:
            cost = clip_s * engine.PRICE_PER_SEC
            if self.max_usd is not None and self.spent + cost > self.max_usd + 1e-9:
                raise OverBudget()
            self.spent += cost

    def tries_on_disk(self, name):
        return sorted(self.work.glob("%s_try*.mp4" % name), key=lambda p: int(p.stem.rsplit("try", 1)[1]))

    def settle(self, c, rec):
        """Keep the best try as the Omni original; finish() voices it into the final take."""
        best = min(rec["tries"], key=lambda t: (len(t.get("failed", ["x"] * 9)), -t["try"])) if rec["tries"] else None
        if best is None or "failed" not in best:
            rec.update(status="flagged", flag=["render"])
            return rec
        src = self.work / ("%s_try%d.mp4" % (c["clip"], best["try"]))
        original = self.work / ("%s.mp4" % c["clip"])
        shutil.copy(src, original)
        rec["kept_try"] = best["try"]
        rec["omni"] = "passed" if not best["failed"] else "flagged"
        rec["omni_failed"] = best["failed"]
        rec.pop("status", None)
        return rec

    def room(self):
        """The video's one room: DIR/_work/room.wav, saved once from the first clip with an Omni original, a
        passed one first, and kept. None while no clip has an original."""
        path = self.work / "room.wav"
        if path.exists():
            return path
        have = [c for c in self.plan["clips"] if (self.work / ("%s.mp4" % c["clip"])).exists()]
        have.sort(key=lambda c: self.report["clips"].get(c["clip"], {}).get("omni") != "passed")
        if not have:
            return None
        return engine.to_wav(self.work / ("%s.mp4" % have[0]["clip"]), path)

    def finish(self, c, rec, room):
        """Voice the clip's Omni original into the final take: re-voiced in the video's room, or kept as is."""
        original = self.work / ("%s.mp4" % c["clip"])
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        voice_flags = []
        if self.voice["mode"] == "native":
            shutil.copy(original, self.final(c["clip"]))
            rec["revoice"] = "native"
        else:
            rv = revoice_mod().revoice_clip(original, c["line"], self.voice, self.final(c["clip"]), self.work, room)
            rec["revoice"] = rv
            voice_flags = ["re-voice: %s" % f for f in rv.get("flag", [])]
        errored = set(rec.get("check_errored", []))
        flags = [("%s (the check errored)" % f) if f in errored else f for f in rec.get("omni_failed", [])] + \
            voice_flags
        rec.update(status="passed" if not flags else "flagged", flag=flags)
        return rec

    def finish_clips(self, clips, fresh):
        """Voice every clip that has an Omni original and no final take, or every one named when fresh."""
        room = None
        if self.voice["mode"] == "elevenlabs":
            room = self.room()
            if room is None:
                return
        todo = []
        for c in clips:
            rec = self.report["clips"].get(c["clip"])
            if not rec or "omni" not in rec:
                continue
            if not fresh and rec.get("status") in ("passed", "flagged") and self.final(c["clip"]).exists():
                continue
            todo.append((c, rec))

        def one(item):
            if self.stop.is_set():
                return
            try:
                self.finish(*item, room)
            except engine.Refused as e:
                self.refused = str(e)
                self.stop.set()
            self.save()
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            list(ex.map(one, todo))

    def render_clip(self, c, fresh):
        name = c["clip"]
        with self.lock:
            rec = self.report["clips"].get(name)
            if fresh or rec is None:
                rec = {"clip": name, "line": c["line"], "clip_s": c["clip_s"], "tries": [], "billed_s": 0.0}
                self.report["clips"][name] = rec
        if fresh:
            for f in self.tries_on_disk(name):
                f.unlink()
        if rec.get("status") in ("passed", "flagged") and self.final(name).exists():
            return
        while len(rec["tries"]) < self.tries and not self.stop.is_set() and not rec.get("check_errored"):
            if any(not t.get("failed") and "failed" in t for t in rec["tries"]):
                break
            k = len(rec["tries"]) + 1
            self.budget(c["clip_s"])
            out = self.work / ("%s_try%d.mp4" % (name, k))
            billed, err = engine.render(c["say"], self.avatar, c["clip_s"], out)
            rec["billed_s"] = round(rec["billed_s"] + billed, 1)
            if err:
                rec["tries"].append({"try": k, "checks": {"render": "FAIL - " + err}})
                self.save()
                continue
            checks = engine.check_clip(out, c["line"], self.avatar, c["clip_s"], self.work)
            rec["tries"].append({"try": k, "checks": engine.summarize(checks), "failed": engine.failed(checks),
                                 "billed_s": round(billed, 1)})
            self.save()
            if engine.only_errored(checks):
                rec["check_errored"] = engine.failed(checks)  # never a paid render for a check that only errored
                break
        if self.stop.is_set():
            return
        if "omni" not in rec or fresh:
            self.settle(c, rec)
        rec["cost_usd"] = round(rec["billed_s"] * engine.PRICE_PER_SEC, 2)
        self.save()

    def worker(self, c, fresh):
        try:
            self.render_clip(c, fresh)
        except engine.Refused as e:
            self.refused = str(e)
            self.stop.set()
        except OverBudget:
            self.over = True
            self.stop.set()

    def outcome(self):
        if self.refused:
            print("refused: %s" % self.refused)
            return 3
        if self.over:
            print("stopped: the next render would pass --max-usd %.2f" % self.max_usd)
            return 5
        names = [c["clip"] for c in self.plan["clips"]]
        flagged = [n for n in names if self.report["clips"].get(n, {}).get("status") != "passed"]
        return 1 if flagged else 0

    def summary(self):
        out = {"clips": [], "billed_s": self.report.get("billed_s"), "cost_usd": self.report.get("cost_usd"),
               "estimate_usd": self.plan["estimate_usd"]}
        for c in self.plan["clips"]:
            r = self.report["clips"].get(c["clip"])
            if r is None:
                continue
            out["clips"].append({"clip": c["clip"], "status": r.get("status", "not done"), "tries": len(r["tries"]),
                                 "flag": r.get("flag", []), "cost_usd": r.get("cost_usd")})
        print(json.dumps(out, indent=1, ensure_ascii=False))


def revoice_mod():
    import revoice
    return revoice


def selected(plan, only):
    if not only:
        return plan["clips"]
    names = {c["clip"] for c in plan["clips"]}
    want = set()
    for x in only:
        if x in names:
            want.add(x)
        elif x.isdigit() and 1 <= int(x) <= len(plan["clips"]):
            want.add(plan["clips"][int(x) - 1]["clip"])
        else:
            raise BadInput("--only names no clip in the plan: %s" % x)
    return [c for c in plan["clips"] if c["clip"] in want]


def check_inputs(run):
    size = engine.probe(run.avatar)[1] if run.avatar.exists() else None
    if not engine.image_ok(size):
        raise BadInput("the avatar image at %s is missing, under 720 wide or not portrait; run image first" % run.avatar)
    if not engine.key("GEMINI_API_KEY"):
        raise BadInput("GEMINI_API_KEY is not set; put it in %s" % engine.keys_file())
    if run.voice["mode"] == "elevenlabs" and not engine.key("ELEVENLABS_API_KEY"):
        raise BadInput("the plan's voice is an ElevenLabs voice and ELEVENLABS_API_KEY is not set; put it in %s, "
                       "or plan again with --voice native" % engine.keys_file())


def cmd_render(d, only, max_usd, tries=engine.MAX_TRIES):
    run = Run(d, max_usd, tries)
    check_inputs(run)
    clips = selected(run.plan, only)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(lambda c: run.worker(c, bool(only)), clips))
    if not run.stop.is_set():
        run.finish_clips(clips, bool(only))
    run.save()
    run.summary()
    return run.outcome()


def cmd_recheck(d, only, tries=engine.MAX_TRIES):
    run = Run(d)
    check_inputs(run)
    try:
        for c in selected(run.plan, only):
            rec = run.report["clips"].get(c["clip"])
            files = run.tries_on_disk(c["clip"])
            if not files:
                continue
            if rec is None:
                rec = run.report["clips"][c["clip"]] = {"clip": c["clip"], "line": c["line"],
                                                        "clip_s": c["clip_s"], "tries": [], "billed_s": 0.0}
            if rec.get("status") == "passed" and run.final(c["clip"]).exists():
                continue
            kept = {t["try"]: t for t in rec["tries"]}
            for f in files:
                k = int(f.stem.rsplit("try", 1)[1])
                checks = engine.check_clip(f, c["line"], run.avatar, c["clip_s"], run.work)
                kept[k] = {"try": k, "checks": engine.summarize(checks), "failed": engine.failed(checks),
                           "billed_s": kept.get(k, {}).get("billed_s", round(engine.probe(f)[0], 1)),
                           "rechecked": True}
            rec["tries"] = [kept[k] for k in sorted(kept)]
            rec.pop("status", None)
            rec.pop("check_errored", None)
            passed = any("failed" in t and not t["failed"] for t in rec["tries"])
            if passed or len(rec["tries"]) >= tries:
                run.settle(c, rec)
                room = run.room() if run.voice["mode"] == "elevenlabs" else None
                if "omni" in rec and (room is not None or run.voice["mode"] == "native"):
                    run.finish(c, rec, room)
                rec["cost_usd"] = round(rec["billed_s"] * engine.PRICE_PER_SEC, 2)
            run.save()
    except engine.Refused as e:
        run.refused = str(e)
    run.save()
    run.summary()
    return run.outcome()


def stitch(d):
    """Join every final take in plan order into DIR/final.mp4. Returns (path, seconds, flagged names);
    raises BadInput when a take is missing."""
    d = Path(d)
    plan_path = d / "_work" / "plan.json"
    if not plan_path.exists():
        raise BadInput("no plan in %s; run plan first" % (d / "_work"))
    plan = json.loads(plan_path.read_text())
    takes = [d / "clips" / ("%s.mp4" % c["clip"]) for c in plan["clips"]]
    missing = [t.stem for t in takes if not t.is_file() or not t.stat().st_size]
    if missing:
        raise BadInput("cannot stitch: no final take for %s" % ", ".join(missing))
    tmp = d / "_work" / "_stitch"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    vf = ("scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=%d,"
          "format=yuv420p" % (engine.WIDTH, engine.HEIGHT, engine.WIDTH, engine.HEIGHT, STITCH_FPS))
    parts = []
    try:
        for k, t in enumerate(takes):
            dur, size, kinds = engine.probe(t)
            if not size or dur <= 0:
                raise BadInput("cannot stitch: %s has no video" % t.stem)
            part = tmp / ("part%04d.mp4" % k)
            audio = "[0:a:0]" if "audio" in kinds else "[1:a]"
            p = engine.ff(["ffmpeg", "-loglevel", "error", "-y", "-i", str(t),
                           "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                           "-filter_complex", "[0:v:0]%s[v];%saresample=48000,aformat=sample_fmts=fltp:"
                                              "channel_layouts=stereo,apad[a]" % (vf, audio),
                           "-map", "[v]", "-map", "[a]", "-t", "%.3f" % dur,
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-profile:v", "high",
                           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(part)])
            if p.returncode or not part.exists():
                raise BadInput("cannot stitch: ffmpeg could not re-encode %s: %s" % (t.stem, p.stderr.strip()[-200:]))
            parts.append(part)
        listing = tmp / "list.txt"
        listing.write_text("".join("file '%s'\n" % pp.resolve().as_posix().replace("'", "'\\''") for pp in parts))
        out = d / "final.mp4"
        p = engine.ff(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                       "-c", "copy", "-movflags", "+faststart", str(out)])
        if p.returncode or not out.exists():
            raise BadInput("cannot stitch: ffmpeg could not join the takes: %s" % p.stderr.strip()[-200:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    report_path = d / "_work" / "report.json"
    rep = json.loads(report_path.read_text()).get("clips", {}) if report_path.exists() else {}
    flagged = [c["clip"] for c in plan["clips"] if rep.get(c["clip"], {}).get("status") != "passed"]
    return out, engine.probe(out)[0], flagged


# ---------------------------------------------------------------- self-test

def self_test():
    import contextlib
    import io
    import tempfile
    fails = []
    p6 = ("That's the Fresh Rinse Pod. Put your own retainer or guard through one cycle. If it doesn't come out "
          "clearer than the brush ever got it, send it back. You've got 120 days for a full refund. And every day "
          "you wait is one more scrub. Plain water, nothing added, nothing scrubbed. The one piece of plastic you "
          "can't give up, finally as clean as the rest of your house.")
    pieces = pack(p6)
    if " ".join(pieces).split() != p6.split():
        fails.append("pack() changed the words")
    if any(len(x.split()) > CAP_WORDS for x in pieces) or len(pieces) != 3:
        fails.append("a long paragraph packs into %s" % [len(x.split()) for x in pieces])
    hook_b = ("If you've cut plastic and harsh chemicals out of your life for your health, but you still scrub your "
              "retainer or night guard with a toothbrush every morning, here are five ways to clean it, ranked.")
    hb = pack(hook_b)
    if [len(x.split()) for x in hb] != [28, 8] or not hb[1].startswith("here are"):
        fails.append("a 36-word sentence cuts into %s, not 28 then 8 at `every morning,`" % [len(x.split()) for x in hb])
    if any(_joiner(x.split()[0]) for x in hb):
        fails.append("a piece opens on a joining word")
    long_colon = ("Health-conscious people who avoid plastic, microplastics and harsh chemicals, but still sleep in a "
                  "plastic retainer, aligner or night guard: here are five ways to clean it, ranked worst to best.")
    lc = pack(long_colon)
    if len(lc) != 2 or not lc[1].startswith("here are"):
        fails.append("a long sentence does not cut at its colon first: %s" % lc)
    if clip_names(3) != ["clip_01", "clip_02", "clip_03"] or clip_names(120)[-1] != "clip_120":
        fails.append("clip_names() does not name clips clip_01, clip_02 ...")
    script = "﻿Hook whole. Second.\r\n\r\nOne. Two UV-C.\n\n\n  " + p6 + "\n"
    if paragraphs(script) != ["Hook whole. Second.", "One. Two UV-C.", p6]:
        fails.append("paragraphs() reads %s" % paragraphs(script))
    voice = read_voice('{"voice_id": "V", "name": "Ava"}')
    if voice != {"mode": "elevenlabs", "voice_id": "V", "name": "Ava", "stability": 0.5, "similarity_boost": 0.75,
                 "style": 0.3}:
        fails.append("read_voice() does not fill the default profile: %s" % voice)
    if read_voice("native") != {"mode": "native"} or read_voice('{"mode": "native"}') != {"mode": "native"}:
        fails.append("read_voice() does not read native")
    for bad in ('{"stability": 0.5}', '{"voice_id": "V", "style": 1.5}', "nonsense", '{"voice_id": "V", "style": "x"}'):
        try:
            read_voice(bad)
            fails.append("read_voice() accepts %s" % bad)
        except BadInput:
            pass
    plan = make_plan(script, voice, respell=read_respell('{"UV-C": "U V C"}'))
    if [c["clip"] for c in plan["clips"]] != ["clip_01", "clip_02", "clip_03", "clip_04", "clip_05"]:
        fails.append("the plan's clips are %s" % [c["clip"] for c in plan["clips"]])
    if [c["paragraph"] for c in plan["clips"]] != [1, 2, 3, 3, 3]:
        fails.append("a clip spans two paragraphs")
    if " ".join(c["line"] for c in plan["clips"]).split() != " ".join(paragraphs(script)).split():
        fails.append("the plan changed the script's words")
    if plan["clips"][1]["say"] != "One. Two U V C." or plan["clips"][1]["line"] != "One. Two UV-C.":
        fails.append("a respelling reached the written line, or missed the said line")
    secs = sum(c["clip_s"] for c in plan["clips"])
    if plan["clip_seconds"] != secs or plan["estimate_usd"] != round(secs * engine.PRICE_PER_SEC, 2) \
            or plan["worst_usd"] != round(secs * engine.PRICE_PER_SEC * engine.MAX_TRIES, 2):
        fails.append("the plan prices %s seconds at %s" % (plan["clip_seconds"], plan["estimate_usd"]))
    if make_plan(script, voice, 1)["worst_usd"] != plan["estimate_usd"]:
        fails.append("the worst case at one try is not the estimate")
    try:
        make_plan(" \n\n ", voice)
        fails.append("an empty script was accepted")
    except BadInput:
        pass
    test = make_plan(TEST_LINE, {"mode": "native"}, names=["test_line"])
    if [c["clip"] for c in test["clips"]] != ["test_line"]:
        fails.append("the test line does not plan as one clip named test_line")
    if [c["clip"] for c in selected(plan, ["2", "clip_04"])] != ["clip_02", "clip_04"]:
        fails.append("--only does not read a clip number and a clip name")
    try:
        selected(plan, ["clip_99"])
        fails.append("--only accepts a clip not in the plan")
    except BadInput:
        pass
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        print_plan(plan)
    shown = buf.getvalue()
    if "said as: One. Two U V C." not in shown or "clips: 5;" not in shown or "up to $" not in shown:
        fails.append("the plan table is missing a line")
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "a.png")
        engine.ff(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=768x1376", "-frames:v",
                   "1", src])
        size, msg = image(d, src)
        if size != (768, 1376) or "too small" in msg or not os.path.exists(os.path.join(d, "_work", "avatar.jpg")):
            fails.append("image() does not convert a portrait image: %s" % msg)
        # stitch: mixed sizes, rates and audio, one take with no sound
        dd = Path(d) / "vid"
        (dd / "_work").mkdir(parents=True)
        (dd / "clips").mkdir()
        two = make_plan("One. Two.\n\nThree four five.\n\nSix.", {"mode": "native"})
        (dd / "_work" / "plan.json").write_text(json.dumps(two))
        specs = [("clip_01", "720x1280", 24, "sine=frequency=300:duration=5:sample_rate=44100"),
                 ("clip_02", "768x1376", 25, None),
                 ("clip_03", "720x1280", 30, "sine=frequency=500:duration=3:sample_rate=24000")]
        for name, s, r, a in specs:
            args = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=%s:d=5:r=%d" % (s, r)]
            if a:
                args += ["-f", "lavfi", "-i", a, "-c:a", "aac"]
            engine.ff(args + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(dd / "clips" / ("%s.mp4" % name))])
        try:
            out, secs, flagged = stitch(dd)
            dur, size, kinds = engine.probe(out)
            if size != (720, 1280) or "audio" not in kinds or abs(dur - 15.0) > 0.3:
                fails.append("stitch() made %s, %.2fs, %s" % (size, dur, kinds))
            if flagged != ["clip_01", "clip_02", "clip_03"]:
                fails.append("stitch() does not name takes with no passed status")
            if (dd / "_work" / "_stitch").exists():
                fails.append("stitch() left its working files")
        except BadInput as e:
            fails.append("stitch() failed on good takes: %s" % e)
        (dd / "clips" / "clip_02.mp4").unlink()
        try:
            stitch(dd)
            fails.append("stitch() ran with a take missing")
        except BadInput as e:
            if "clip_02" not in str(e):
                fails.append("the missing-take line does not name the take")
    parser = build_parser()
    if parser.parse_args(["render", "D"]).tries != engine.MAX_TRIES or parser.parse_args(["recheck", "D"]).tries != engine.MAX_TRIES:
        fails.append("--tries does not default to the engine's most")
    if parser.parse_args(["render", "D", "--tries", "1"]).tries != 1 or \
            parser.parse_args(["plan", "D", "--script", "s", "--voice", "native", "--tries", "2"]).tries != 2:
        fails.append("--tries does not read its number")
    if parser.parse_args(["plan", "D", "--test-line", "--voice", "native"]).test_line != TEST_LINE:
        fails.append("--test-line with no text does not plan the standard test line")
    for bad in ("0", "4", "-1", "one", "1.5"):
        code = None
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                parser.parse_args(["render", "D", "--tries", bad])
            except SystemExit as e:
                code = e.code
        if code != 2:
            fails.append("--tries %s is not refused as bad input" % bad)
    for f in fails:
        print("run.py self-test: %s" % f)
    print("run.py self-test: %s" % ("pass" if not fails else "fail"))
    return 0 if not fails else 1


def tries_arg(value):
    """--tries: a whole number of tries a clip, from 1 to the engine's most."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not a whole number of tries" % value)
    if not 1 <= n <= engine.MAX_TRIES:
        raise argparse.ArgumentTypeError("%d is outside 1 to %d tries a clip" % (n, engine.MAX_TRIES))
    return n


def build_parser():
    ap = argparse.ArgumentParser(prog="run.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight")
    p = sub.add_parser("image")
    p.add_argument("dir")
    p.add_argument("file")
    p = sub.add_parser("plan")
    p.add_argument("dir")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--script")
    src.add_argument("--test-line", nargs="?", const=TEST_LINE)
    p.add_argument("--voice", required=True)
    p.add_argument("--respell")
    p.add_argument("--tries", type=tries_arg, default=engine.MAX_TRIES)
    for name in ("render", "recheck"):
        p = sub.add_parser(name)
        p.add_argument("dir")
        p.add_argument("--only", nargs="+")
        p.add_argument("--tries", type=tries_arg, default=engine.MAX_TRIES)
        if name == "render":
            p.add_argument("--max-usd", type=float)
    p = sub.add_parser("stitch")
    p.add_argument("dir")
    return ap


def main(argv):
    if argv[:1] == ["--self-test"]:
        return self_test()
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "preflight":
            missing, notes = preflight()
            for n in notes:
                print("preflight: %s" % n)
            for m in missing:
                print("preflight: %s is missing" % m)
            print("preflight: %s" % ("pass" if not missing else "fail"))
            return 0 if not missing else 2
        if a.cmd == "image":
            size, msg = image(a.dir, a.file)
            print("image: %s" % msg)
            return 0 if engine.image_ok(size) else 1
        if a.cmd == "plan":
            voice = read_voice(a.voice)
            respell = read_respell(a.respell)
            if a.test_line is not None:
                plan = make_plan(a.test_line, voice, a.tries, respell, names=["test_line"], source="test line")
            else:
                sp = Path(a.script)
                if not sp.is_file():
                    raise BadInput("no script file at %s" % sp)
                try:
                    text = sp.read_text(encoding="utf-8-sig")
                except UnicodeDecodeError:
                    raise BadInput("the script file is not UTF-8 text")
                plan = make_plan(text, voice, a.tries, respell, source=sp.name)
            work = Path(a.dir) / "_work"
            work.mkdir(parents=True, exist_ok=True)
            (work / "plan.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False))
            print_plan(plan)
            return 0
        if a.cmd == "stitch":
            out, secs, flagged = stitch(a.dir)
            print("stitch: %d takes, %.1fs, %s" % (len(json.loads((Path(a.dir) / "_work" / "plan.json")
                                                                  .read_text())["clips"]), secs, out))
            if flagged:
                print("stitch: includes takes not passed: %s" % ", ".join(flagged))
            return 0
        if not (Path(a.dir) / "_work" / "plan.json").exists():
            raise BadInput("no plan in %s; run plan first" % (Path(a.dir) / "_work"))
        if a.cmd == "render":
            return cmd_render(a.dir, a.only, a.max_usd, a.tries)
        return cmd_recheck(a.dir, a.only, a.tries)
    except BadInput as e:
        print("run.py: %s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
