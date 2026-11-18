#!/usr/bin/env python3
"""Avatar library: the user's saved avatars, each one image and, optionally, one paired ElevenLabs voice.

Usage:
    python3 scripts/library.py list
    python3 scripts/library.py add NAME IMAGE [--replace]
    python3 scripts/library.py pair NAME VOICE_ID [--stability N] [--similarity N] [--style N]
    python3 scripts/library.py unpair NAME
    python3 scripts/library.py remove NAME
    python3 scripts/library.py show NAME
    python3 scripts/library.py --self-test

The library lives at ~/.avatar-studio/library/<NAME>/ (AVATAR_STUDIO_HOME moves it): avatar.jpg, the
image, and pair.json, the paired voice. NAME is 1 to 40 letters, digits, - or _, starting with a letter
or digit.

list    every avatar: its name, image size, and paired voice (name and id) or none.
add     converts IMAGE to the avatar's avatar.jpg and checks it is portrait and at least 720 wide.
        A name already in the library is refused unless --replace; a replace keeps the paired voice.
pair    checks VOICE_ID on the user's ElevenLabs account (GET /v1/voices/{id}), then stores it with
        its name and its profile: --stability, --similarity, --style, each 0 to 1, defaulting to
        0.5, 0.75 and 0.3.
unpair  drops the avatar's paired voice; the avatar then renders in Omni's native voice.
remove  deletes the avatar's folder, its image and its pairing.
show    prints the voice JSON run.py plan --voice takes, or `native` when no voice is paired.

ELEVENLABS_API_KEY is read only by pair, never printed. Exit: 0 done; 1 the image or the voice failed
its check; 2 bad input; 3 ElevenLabs refused (key missing or rejected).
--self-test runs every command on a scratch library with the voice lookup stubbed; no network call.
"""

import argparse
import json
import os
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
VOICE_URL = "https://api.elevenlabs.io/v1/voices/%s"
DEFAULTS = {"stability": 0.5, "similarity_boost": 0.75, "style": 0.3}


class BadInput(Exception):
    pass


class CheckFailed(Exception):
    pass


def lib_dir():
    return engine.home() / "library"


def avatar_dir(name, must_exist=True):
    if not NAME_RE.match(name or ""):
        raise BadInput("%r is not a library name: 1 to 40 letters, digits, - or _" % name)
    d = lib_dir() / name
    if must_exist and not (d / "avatar.jpg").is_file():
        raise BadInput("no avatar named %s in the library" % name)
    return d


def read_pair(d):
    p = Path(d) / "pair.json"
    try:
        return json.loads(p.read_text()) if p.is_file() else None
    except ValueError:
        return None


def entries():
    """[(name, (w, h) or None, pair or None)] for every avatar in the library, by name."""
    out = []
    if lib_dir().is_dir():
        for d in sorted(lib_dir().iterdir(), key=lambda p: p.name.lower()):
            if d.is_dir() and (d / "avatar.jpg").is_file():
                out.append((d.name, engine.probe(d / "avatar.jpg")[1], read_pair(d)))
    return out


def cmd_list():
    rows = entries()
    print("library: %s" % lib_dir())
    if not rows:
        print("library: no avatars yet; add one with: library.py add NAME IMAGE")
        return 0
    w = max(len(r[0]) for r in rows)
    for name, size, pair in rows:
        voice = "none (native voice)" if not pair else "%s (%s)" % (pair.get("name") or "unnamed", pair["voice_id"])
        print("%-*s  %-10s  voice: %s" % (w, name, "%dx%d" % size if size else "unreadable", voice))
    return 0


def cmd_add(name, src, replace=False):
    d = avatar_dir(name, must_exist=False)
    if (d / "avatar.jpg").exists() and not replace:
        raise BadInput("an avatar named %s is already in the library; add --replace to replace its image" % name)
    if not Path(src).is_file():
        raise BadInput("no image file at %s" % src)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "_new.jpg"
    size, msg = engine.convert_image(src, tmp)
    if not engine.image_ok(size):
        tmp.unlink(missing_ok=True)
        if not any(d.iterdir()):
            d.rmdir()
        raise CheckFailed("the image is %s; it must be portrait and at least %d wide" % (msg, engine.WIDTH))
    os.replace(tmp, d / "avatar.jpg")
    print("library: %s %s, %s" % ("replaced" if replace else "added", name, msg))
    return 0


def fetch_voice(voice_id):
    """(voice name) for a voice id on the user's ElevenLabs account. Raises engine.Refused or CheckFailed."""
    k = engine.key("ELEVENLABS_API_KEY")
    if not k:
        raise engine.Refused("ElevenLabs: key missing: set ELEVENLABS_API_KEY in %s" % engine.keys_file())
    code, body = engine.http_get(VOICE_URL % urllib.parse.quote(voice_id, safe=""), {"xi-api-key": k})
    body = (body or "").replace(k, "<key>")
    if code == 200:
        try:
            return json.loads(body).get("name") or ""
        except ValueError:
            raise CheckFailed("ElevenLabs answered with something that is not a voice")
    low = body.lower()
    if code == 401 and "missing_permissions" in low:
        raise engine.Refused("ElevenLabs: key rejected: it lacks the voices read permission")
    if code == 401 or "invalid_api_key" in low:
        raise engine.Refused("ElevenLabs: key rejected")
    if code in (400, 404, 422):
        raise CheckFailed("ElevenLabs has no voice %s on this account; a Voice Library voice may need adding "
                          "to My Voices first" % voice_id)
    raise CheckFailed("ElevenLabs could not be asked about the voice: %s" % (
        ("HTTP %d" % code) if code else body[:160]))


def cmd_pair(name, voice_id, stability, similarity, style):
    d = avatar_dir(name)
    voice_id = (voice_id or "").strip()
    if not voice_id or not re.match(r"^[A-Za-z0-9_-]{1,64}$", voice_id):
        raise BadInput("%r is not an ElevenLabs voice id" % voice_id)
    profile = {"stability": stability, "similarity_boost": similarity, "style": style}
    for k, v in profile.items():
        if not 0 <= v <= 1:
            raise BadInput("%s is %s, not between 0 and 1" % (k, v))
    voice_name = fetch_voice(voice_id)
    pair = dict({"voice_id": voice_id, "name": voice_name}, **profile)
    (d / "pair.json").write_text(json.dumps(pair, indent=1, ensure_ascii=False))
    print("library: %s paired with %s (%s), stability %s, similarity %s, style %s" % (
        name, voice_name or "unnamed", voice_id, stability, similarity, style))
    return 0


def cmd_unpair(name):
    p = avatar_dir(name) / "pair.json"
    if p.exists():
        p.unlink()
        print("library: %s unpaired; it renders in the native voice" % name)
    else:
        print("library: %s had no paired voice" % name)
    return 0


def cmd_remove(name):
    d = avatar_dir(name)
    shutil.rmtree(d)
    print("library: removed %s" % name)
    return 0


def cmd_show(name):
    pair = read_pair(avatar_dir(name))
    if not pair or not pair.get("voice_id"):
        print("library: %s has no paired voice" % name, file=sys.stderr)
        print("native")
        return 0
    out = {"voice_id": pair["voice_id"], "name": pair.get("name") or ""}
    for k, default in DEFAULTS.items():
        out[k] = pair.get(k, default)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def unit(value):
    try:
        return float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not a number" % value)


def build_parser():
    ap = argparse.ArgumentParser(prog="library.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("add")
    p.add_argument("name")
    p.add_argument("image")
    p.add_argument("--replace", action="store_true")
    p = sub.add_parser("pair")
    p.add_argument("name")
    p.add_argument("voice_id")
    p.add_argument("--stability", type=unit, default=DEFAULTS["stability"])
    p.add_argument("--similarity", type=unit, default=DEFAULTS["similarity_boost"])
    p.add_argument("--style", type=unit, default=DEFAULTS["style"])
    for name in ("unpair", "remove", "show"):
        sub.add_parser(name).add_argument("name")
    return ap


def main(argv):
    if argv[:1] == ["--self-test"]:
        return self_test()
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "list":
            return cmd_list()
        if a.cmd == "add":
            return cmd_add(a.name, a.image, a.replace)
        if a.cmd == "pair":
            return cmd_pair(a.name, a.voice_id, a.stability, a.similarity, a.style)
        return {"unpair": cmd_unpair, "remove": cmd_remove, "show": cmd_show}[a.cmd](a.name)
    except BadInput as e:
        print("library.py: %s" % e)
        return 2
    except CheckFailed as e:
        print("library.py: %s" % e)
        return 1
    except engine.Refused as e:
        print("library.py: refused: %s" % e)
        return 3


# ---------------------------------------------------------------- self-test

def self_test():
    import contextlib
    import io
    import tempfile
    fails = []
    saved_home = os.environ.pop("AVATAR_STUDIO_HOME", None)
    saved_fetch = globals()["fetch_voice"]
    try:
        with tempfile.TemporaryDirectory() as d:
            os.environ["AVATAR_STUDIO_HOME"] = os.path.join(d, "home")
            tall, wide = os.path.join(d, "t.png"), os.path.join(d, "w.png")
            engine.ff(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=768x1376",
                       "-frames:v", "1", tall])
            engine.ff(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=1280x720",
                       "-frames:v", "1", wide])

            def run(*argv):
                with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                    try:
                        code = main(list(argv))
                    except SystemExit as e:  # argparse refusing an argument
                        code = e.code
                return code, out.getvalue()

            if run("list")[0] != 0 or "no avatars yet" not in run("list")[1]:
                fails.append("list does not report an empty library")
            if run("add", "ava", tall)[0] != 0 or not (lib_dir() / "ava" / "avatar.jpg").is_file():
                fails.append("add does not save a portrait image")
            if run("add", "ava", tall)[0] != 2:
                fails.append("add accepts a duplicate name")
            if run("add", "wide", wide)[0] != 1 or (lib_dir() / "wide").exists():
                fails.append("add keeps a landscape image")
            for bad in ("../x", "", "a b", "-x", "x" * 41):
                if run("add", bad, tall)[0] != 2:
                    fails.append("add accepts the name %r" % bad)
            if run("show", "ava") != (0, "native\n"):
                fails.append("show does not print native for an unpaired avatar")
            globals()["fetch_voice"] = lambda vid: "Test Voice"
            if run("pair", "ava", "abc123", "--stability", "0.4")[0] != 0:
                fails.append("pair does not store a voice")
            code, out = run("show", "ava")
            if code != 0 or json.loads(out) != {"voice_id": "abc123", "name": "Test Voice", "stability": 0.4,
                                                 "similarity_boost": 0.75, "style": 0.3}:
                fails.append("show prints %r" % out)
            if run("pair", "ava", "abc123", "--style", "2")[0] != 2:
                fails.append("pair accepts a style over 1")
            if run("pair", "nobody", "abc123")[0] != 2:
                fails.append("pair accepts an avatar not in the library")

            def refuse(vid):
                raise engine.Refused("ElevenLabs: key rejected")
            globals()["fetch_voice"] = refuse
            if run("pair", "ava", "abc123")[0] != 3:
                fails.append("pair does not exit 3 when ElevenLabs refuses")
            if run("add", "ava", tall, "--replace")[0] != 0 or not (lib_dir() / "ava" / "pair.json").exists():
                fails.append("add --replace drops the paired voice")
            code, out = run("list")
            if "Test Voice (abc123)" not in out or "768x1376" not in out:
                fails.append("list prints %r" % out)
            if run("unpair", "ava")[0] != 0 or run("show", "ava")[1] != "native\n":
                fails.append("unpair does not drop the voice")
            if run("remove", "ava")[0] != 0 or (lib_dir() / "ava").exists():
                fails.append("remove does not delete the avatar")
            if run("remove", "ava")[0] != 2:
                fails.append("remove accepts an avatar not in the library")
            globals()["fetch_voice"] = saved_fetch
            env_key = os.environ.pop("ELEVENLABS_API_KEY", None)
            try:
                run("add", "bo", tall)
                if run("pair", "bo", "abc123")[0] != 3:
                    fails.append("pair runs with no ElevenLabs key")
            finally:
                if env_key is not None:
                    os.environ["ELEVENLABS_API_KEY"] = env_key
    finally:
        globals()["fetch_voice"] = saved_fetch
        os.environ.pop("AVATAR_STUDIO_HOME", None)
        if saved_home is not None:
            os.environ["AVATAR_STUDIO_HOME"] = saved_home
    for f in fails:
        print("library.py self-test: %s" % f)
    print("library.py self-test: %s" % ("pass" if not fails else "fail"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
