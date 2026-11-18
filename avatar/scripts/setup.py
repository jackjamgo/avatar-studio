#!/usr/bin/env python3
"""Avatar Studio setup: the keys file, a live check of each key, and the tools this machine still needs.

Usage:
    python3 scripts/setup.py init
    python3 scripts/setup.py open
    python3 scripts/setup.py check
    python3 scripts/setup.py tools
    python3 scripts/setup.py --self-test

init   creates ~/.avatar-studio/ (AVATAR_STUDIO_HOME moves it), its library/ folder, and keys.env with
       a commented placeholder line for GEMINI_API_KEY and ELEVENLABS_API_KEY, readable by the user
       only. A keys.env already there is never overwritten: only a missing key's line is added.
open   opens keys.env in the default text editor (TextEdit on macOS, Notepad on Windows, xdg-open
       elsewhere), running init first when it is not there yet.
check  which keys are set and where (keys.env or the environment), then one cheap live call each:
       Gemini lists one model, ElevenLabs reads the account (GET /v1/user). Reports ok, rejected,
       missing or could not check. Never prints a key. Exit 0 when Gemini is ok and ElevenLabs is ok
       or missing; 1 otherwise.
tools  python 3.9+ and imageio-ffmpeg, which carries its own ffmpeg: what is missing, and the one
       line that installs it, `python3 -m pip install --user imageio-ffmpeg` (without --user inside a
       virtual environment; with --break-system-packages on a Python that marks itself externally
       managed). Python 3.9 or newer is missing: `xcode-select --install`, macOS's own one-click
       install, which brings python3. Exit 0 when nothing is missing, 1 otherwise. Mac only for now.

On a Mac with no python3 at all this script cannot run: `xcode-select --install` is the fix.

--self-test runs init on a scratch folder, reads the check's answers from canned replies and builds
the install line for each case; no network call.
"""

import importlib.util
import os
import platform
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402

GEMINI_CHECK = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"
ELEVEN_CHECK = "https://api.elevenlabs.io/v1/user"

TEMPLATE = """# Avatar Studio keys. One KEY=VALUE line each. Keep this file private; never share or commit it.
# Replace each placeholder with your own key and save. Lines starting with # are ignored.

# Gemini (required): create a key in Google AI Studio, https://aistudio.google.com/apikey
# The key's project needs billing turned on: the video model has no free tier.
GEMINI_API_KEY=paste-your-gemini-key-here

# ElevenLabs (optional): re-voices every clip in a voice from your ElevenLabs account.
# Leave the placeholder to use the avatar's own voice instead.
ELEVENLABS_API_KEY=paste-your-elevenlabs-key-here
"""

PLACEHOLDER_LINES = {
    "GEMINI_API_KEY": "GEMINI_API_KEY=paste-your-gemini-key-here",
    "ELEVENLABS_API_KEY": "ELEVENLABS_API_KEY=paste-your-elevenlabs-key-here",
}


# ---------------------------------------------------------------- init and open

def _private(path, mode):
    try:
        os.chmod(path, mode)
    except OSError:
        pass  # Windows keeps its own file permissions


def init():
    """Create the folder, the library and keys.env. Returns the lines it reports."""
    home = engine.home()
    said = []
    if not home.exists():
        home.mkdir(parents=True)
        said.append("created %s" % home)
    _private(home, 0o700)
    (home / "library").mkdir(exist_ok=True)
    kf = engine.keys_file()
    if not kf.exists():
        kf.write_text(TEMPLATE, encoding="utf-8")
        said.append("created %s with a placeholder line for each key" % kf)
    else:
        text = kf.read_text(encoding="utf-8-sig")
        present = {ln.split("=", 1)[0].strip().replace("export ", "").strip()
                   for ln in text.splitlines() if "=" in ln and not ln.strip().startswith("#")}
        add = [n for n in engine.KEY_NAMES if n not in present]
        if add:
            sep = "" if text.endswith("\n") or not text else "\n"
            kf.write_text(text + sep + "".join(PLACEHOLDER_LINES[n] + "\n" for n in add), encoding="utf-8")
            said.append("kept %s as it was and added a placeholder line for %s" % (kf, ", ".join(add)))
        else:
            said.append("%s is already there; nothing changed" % kf)
    _private(kf, 0o600)
    return said


def open_cmd(path, system=None):
    system = system or platform.system()
    if system == "Darwin":
        return ["open", "-e", str(path)]
    if system == "Windows":
        return ["notepad", str(path)]
    return ["xdg-open", str(path)]


def cmd_open():
    if not engine.keys_file().exists():
        for ln in init():
            print("setup: %s" % ln)
    cmd = open_cmd(engine.keys_file())
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        print("setup: could not open an editor; open %s by hand" % engine.keys_file())
        return 1
    print("setup: opened %s; paste each key over its placeholder, save, and close it" % engine.keys_file())
    return 0


# ---------------------------------------------------------------- check

def read_gemini(code, body):
    b = (body or "").lower()
    if code == 200:
        return "ok", "the key works (billing is not checked here: the video model needs a billed project)"
    if code is None:
        return "could not check", "no answer: %s" % (body or "")[:120]
    if code in (400, 401, 403) and ("api key" in b or "api_key" in b or "permission_denied" in b
                                    or "unauthenticated" in b):
        return "rejected", "Google refused the key (HTTP %d)" % code
    return "could not check", "HTTP %d" % code


def read_eleven(code, body):
    b = (body or "").lower()
    if code == 200:
        return "ok", "the key works"
    if code is None:
        return "could not check", "no answer: %s" % (body or "")[:120]
    if "missing_permissions" in b:
        return "ok", ("the key is a restricted key that cannot read the account; make sure it allows "
                      "Speech to Speech and reading Voices")
    if code == 401 or "invalid_api_key" in b:
        return "rejected", "ElevenLabs refused the key (HTTP %d)" % code
    return "could not check", "HTTP %d" % code


def check(get=None):
    """[(name, status, note)] for each key. `get` is the GET to use; the self-test passes a stub."""
    get = get or engine.http_get
    out = []
    for name, url, header, reader in (("GEMINI_API_KEY", GEMINI_CHECK, "x-goog-api-key", read_gemini),
                                      ("ELEVENLABS_API_KEY", ELEVEN_CHECK, "xi-api-key", read_eleven)):
        k = engine.key(name)
        if not k:
            note = "required: put it in %s" % engine.keys_file() if name == "GEMINI_API_KEY" else \
                "optional: without it only the native voice runs"
            out.append((name, "missing", note))
            continue
        code, body = get(url, {header: k})
        status, note = reader(code, (body or "").replace(k, "<key>"))
        out.append((name, status, "%s; read from %s" % (note, engine.key_source(name))))
    return out


def check_exit(rows):
    st = {n: s for n, s, _ in rows}
    return 0 if st.get("GEMINI_API_KEY") == "ok" and st.get("ELEVENLABS_API_KEY") in ("ok", "missing") else 1


# ---------------------------------------------------------------- tools

def missing_tools():
    miss = []
    if sys.version_info < (3, 9):
        miss.append("python")
    if importlib.util.find_spec("imageio_ffmpeg") is None:
        miss.append("imageio-ffmpeg")
    return miss


def install_command(miss, venv=None, managed=None):
    """The one line that installs what is missing, or None when nothing is."""
    if "python" in miss:
        return "xcode-select --install"
    if "imageio-ffmpeg" not in miss:
        return None
    return engine.install_line(venv, managed)


def ffmpeg_version(exe):
    try:
        out = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, text=True, errors="replace",
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.split("\n", 1)[0].replace("ffmpeg version ", "").split(" ")[0] if out.startswith("ffmpeg") else None


def cmd_tools():
    miss = missing_tools()
    print("setup: python %d.%d at %s" % (sys.version_info[0], sys.version_info[1], sys.executable))
    if "python" in miss:
        print("setup: python 3.9 or newer is missing: macOS's own install brings it; then run /usr/bin/python3")
    print("setup: imageio-ffmpeg %s" % ("missing" if "imageio-ffmpeg" in miss else "found"))
    exe = engine.ffmpeg_exe()
    ver = ffmpeg_version(exe) if exe else None
    if exe and ver:
        print("setup: ffmpeg %s at %s" % (ver, exe))
    elif exe:
        print("setup: ffmpeg at %s does not run" % exe)
    cmd = install_command(miss)
    if not cmd and not ver:
        cmd = install_command(["imageio-ffmpeg"])
    if not cmd:
        print("setup: tools: nothing missing")
        return 0
    print("setup: install with:")
    print(cmd)
    return 1


# ---------------------------------------------------------------- self-test

def self_test():
    import tempfile
    fails = []
    saved = {n: os.environ.pop(n, None) for n in engine.KEY_NAMES + ("AVATAR_STUDIO_HOME",)}
    try:
        with tempfile.TemporaryDirectory() as d:
            os.environ["AVATAR_STUDIO_HOME"] = os.path.join(d, "home")
            said = init()
            kf = engine.keys_file()
            if not kf.exists() or not (engine.home() / "library").is_dir() or not any("created" in s for s in said):
                fails.append("init does not create the folder, the library and keys.env")
            if engine.read_keys_file() != {}:
                fails.append("the template's placeholders read as keys")
            if os.name == "posix" and (kf.stat().st_mode & 0o777) != 0o600:
                fails.append("keys.env is not readable by the user only")
            kf.write_text("# mine\nGEMINI_API_KEY=real_gemini_value_123\n")
            init()
            text = kf.read_text()
            if "real_gemini_value_123" not in text or "ELEVENLABS_API_KEY=paste" not in text \
                    or text.count("GEMINI_API_KEY=") != 1:
                fails.append("init overwrote a real value or did not add the missing line")
            before = kf.read_text()
            if "nothing changed" not in " ".join(init()) or kf.read_text() != before:
                fails.append("init changes a complete keys.env")
            calls = []

            def stub(url, headers):
                calls.append((url, sorted(headers)))
                return 200, "{}"
            rows = check(stub)
            st = {n: s for n, s, _ in rows}
            if st != {"GEMINI_API_KEY": "ok", "ELEVENLABS_API_KEY": "missing"} or check_exit(rows) != 0:
                fails.append("check reads %s" % st)
            if calls != [(GEMINI_CHECK, ["x-goog-api-key"])]:
                fails.append("check called %s" % calls)
            if any("real_gemini_value_123" in note for _, _, note in rows):
                fails.append("check prints a key")
            kf.write_text("GEMINI_API_KEY=real_gemini_value_123\nELEVENLABS_API_KEY=real_eleven_value_456\n")
            rows = check(lambda url, h: (401, '{"detail":{"status":"invalid_api_key"}}') if "elevenlabs" in url
                         else (400, '{"error":{"message":"API key not valid."}}'))
            if [s for _, s, _ in rows] != ["rejected", "rejected"] or check_exit(rows) != 1:
                fails.append("check does not read two rejected keys: %s" % rows)
            rows = check(lambda url, h: (None, "URLError: no network"))
            if [s for _, s, _ in rows] != ["could not check", "could not check"]:
                fails.append("check does not read no network as could not check")
            if read_eleven(401, '{"detail":{"status":"missing_permissions"}}')[0] != "ok":
                fails.append("a restricted ElevenLabs key reads as rejected")
    finally:
        for n, v in saved.items():
            os.environ.pop(n, None)
            if v is not None:
                os.environ[n] = v
    if install_command([], False, False) is not None:
        fails.append("tools offers a command when nothing is missing")
    if install_command(["imageio-ffmpeg"], False, False) != "python3 -m pip install --user imageio-ffmpeg":
        fails.append("tools does not offer the one pip line for imageio-ffmpeg")
    if install_command(["imageio-ffmpeg"], True, False) != "python3 -m pip install imageio-ffmpeg":
        fails.append("tools offers --user inside a virtual environment")
    if install_command(["imageio-ffmpeg"], False, True) != \
            "python3 -m pip install --user --break-system-packages imageio-ffmpeg":
        fails.append("tools does not get past an externally managed Python")
    if install_command(["python", "imageio-ffmpeg"], False, False) != "xcode-select --install":
        fails.append("tools does not offer macOS's own install for a missing python")
    if any(w in s.lower() for s in (install_command(["imageio-ffmpeg"], v, m) for v in (0, 1) for m in (0, 1))
           for w in ("brew", "winget", "numpy", "ffprobe")):
        fails.append("an install line still names Homebrew, winget, numpy or ffprobe")
    exe = engine.ffmpeg_exe()
    if not exe or not ffmpeg_version(exe):
        fails.append("no ffmpeg runs here: %s" % (exe or "none found"))
    if open_cmd("k", "Darwin") != ["open", "-e", "k"] or open_cmd("k", "Windows") != ["notepad", "k"] \
            or open_cmd("k", "Linux") != ["xdg-open", "k"]:
        fails.append("open does not pick the editor for each system")
    for f in fails:
        print("setup.py self-test: %s" % f)
    print("setup.py self-test: %s" % ("pass" if not fails else "fail"))
    return 0 if not fails else 1


def main(argv):
    if argv[:1] == ["--self-test"]:
        return self_test()
    if len(argv) != 1 or argv[0] not in ("init", "open", "check", "tools"):
        print(__doc__)
        return 0 if argv[:1] in (["-h"], ["--help"]) else 2
    if argv[0] == "init":
        for ln in init():
            print("setup: %s" % ln)
        return 0
    if argv[0] == "open":
        return cmd_open()
    if argv[0] == "check":
        rows = check()
        for name, status, note in rows:
            print("setup: %s: %s (%s)" % (name, status, note))
        return check_exit(rows)
    return cmd_tools()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
