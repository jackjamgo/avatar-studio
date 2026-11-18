#!/usr/bin/env python3
"""Room match: make a dry re-voiced take sound recorded in the video's one room, MEDIUM on every take.

Usage:
    python3 scripts/roommatch.py ROOM.wav DRY_AUDIO OUT.wav
    python3 scripts/roommatch.py --self-test

ROOM is the video's room audio, one Omni clip's own audio; DRY_AUDIO is the voice to place in that
room; OUT is written as 16-bit mono WAV at 44.1 kHz, the length of DRY_AUDIO.

1. Tone match: the speech in each file is measured in third-octave bands (ffmpeg bandpass filters and
   astats), and the new voice is shaped to the room audio's balance (mic and room colour) with a
   linear-phase ffmpeg firequalizer, each band held within 12 dB.
2. Small-room echo: a short synthetic room impulse response, convolved with ffmpeg's afir and mixed
   in at MEDIUM's `wet`.
3. Level match: speech loudness set to the room audio's.

No room tone is laid underneath: the pauses stay silent. MEDIUM was picked by ear. The processing is
ffmpeg's filters; Python only measures and sets the numbers, with the standard library alone. ffmpeg
comes from engine.ffmpeg_exe(): imageio-ffmpeg's, else one on PATH. Makes no network call.
Exits 0 on success, 1 on a failed self-test, 2 on bad input.
"""

import array
import math
import os
import re
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402

SR = 44100
MEDIUM = {"wet": 0.18, "rt60": 0.35}
HOP = 1024                    # speech frames are 2048 samples at a 1024 hop
GATE_DB = 30                  # a frame is speech when within this many dB of the loudest frame
MAX_DB = 12                   # the tone match moves no band further than this
BANDS = [1000 * 2 ** (k / 3.0) for k in range(-16, 14)]   # third-octave centres, 25 Hz to 20 kHz
BAND_Q = 3.0                  # three of these biquads in a row make one band, about a third octave wide
EQ_LAG = 1024                 # firequalizer's half length in samples: a 2048-tap linear-phase filter


# ---------------------------------------------------------------- audio in and out

def _f32(data):
    a = array.array("f")
    a.frombytes(data[: len(data) - len(data) % 4])
    if sys.byteorder == "big":
        a.byteswap()
    return a


def _bytes(a):
    if sys.byteorder == "big":
        a = array.array("f", a)
        a.byteswap()
    return a.tobytes()


def _run(args, data=None):
    p = engine.ff(args, text=False, input=data)
    if p.returncode:
        raise ValueError("ffmpeg failed: %s" % p.stderr.decode("utf-8", "replace").strip()[-300:])
    return p


def load(path):
    """The file's audio as mono float samples at SR."""
    return _f32(_run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", str(SR),
                      "-f", "f32le", "-"]).stdout)


def save(path, x):
    """Write float samples as 16-bit mono WAV at SR, clipped to full scale."""
    _run(["ffmpeg", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
          "-c:a", "pcm_s16le", str(path)], _bytes(array.array("f", x)))


# ---------------------------------------------------------------- speech frames

def _block_ms(x):
    """Mean square of each HOP-sample block, the last one zero-padded."""
    out = []
    for i in range(0, max(len(x), 1), HOP):
        blk = x[i:i + HOP]
        out.append(math.fsum(v * v for v in blk) / HOP)
    return out


def _frames_ms(blocks):
    """Mean square of each 2048-sample frame at a 1024 hop: two blocks each."""
    if len(blocks) < 2:
        return [sum(blocks) / 2.0]
    return [(blocks[k] + blocks[k + 1]) / 2.0 for k in range(len(blocks) - 1)]


def speech_mask(x):
    """([speech or not per frame], [frame level in dB])."""
    db = [10 * math.log10(m + 1e-12) for m in _frames_ms(_block_ms(x))]
    top = max(db)
    return [d > top - GATE_DB for d in db], db


def speech_rms(x):
    """RMS over the speech frames alone, so pauses and their length do not pull the level down."""
    ms = _frames_ms(_block_ms(x))
    top = 10 * math.log10(max(ms) + 1e-12)
    sel = [m for m in ms if 10 * math.log10(m + 1e-12) > top - GATE_DB]
    return math.sqrt(sum(sel) / len(sel) + 1e-12)


def gate(x):
    """x with every block outside a speech frame set to silence, for measuring the speech's tone."""
    mask, _ = speech_mask(x)
    y = array.array("f", x)
    zero = array.array("f", bytes(4 * HOP))
    for b in range((len(x) + HOP - 1) // HOP):
        if not ((b - 1 >= 0 and b - 1 < len(mask) and mask[b - 1]) or (b < len(mask) and mask[b])):
            n = len(y[b * HOP:(b + 1) * HOP])
            y[b * HOP:b * HOP + n] = zero[:n]
    return y


# ---------------------------------------------------------------- 1. tone match

def band_levels(x):
    """Level in dB of each third-octave band of x, measured by ffmpeg."""
    n = len(BANDS)
    bp = "bandpass=f=%.2f:width_type=q:width=%.2f"
    graph = "asplit=%d%s;" % (n, "".join("[s%d]" % i for i in range(n)))
    graph += ";".join("[s%d]%s,astats[o%d]" % (i, ",".join([bp % (f, BAND_Q)] * 3), i) for i, f in enumerate(BANDS))
    graph += ";%samix=inputs=%d[out]" % ("".join("[o%d]" % i for i in range(n)), n)
    err = _run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-f", "f32le", "-ar", str(SR), "-ac", "1",
                "-i", "-", "-filter_complex", graph, "-map", "[out]", "-f", "null", "-"], _bytes(x)).stderr
    level = {}
    for tag, val in re.findall(r"\[Parsed_astats_(\d+) @ [^\]]+\] RMS level dB: (\S+)",
                               err.decode("utf-8", "replace")):
        level[int(tag)] = -200.0 if "inf" in val else float(val)  # the last line, the Overall one, wins
    if len(level) != n:
        raise ValueError("ffmpeg measured %d of %d tone bands" % (len(level), n))
    return [level[k] for k in sorted(level)]


def tone_gains(room, dry, max_db=MAX_DB):
    """The gain in dB per band that turns the dry voice's speech balance into the room audio's: shape
    only, the median band at 0 dB; level is matched later."""
    g = [max(-max_db, min(max_db, r - d)) for r, d in zip(band_levels(gate(room)), band_levels(gate(dry)))]
    mid = sorted(g)[len(g) // 2] if len(g) % 2 else sum(sorted(g)[len(g) // 2 - 1: len(g) // 2 + 1]) / 2.0
    return [v - mid for v in g]


def eq_curve(gains, step=1 / 6.0):
    """[(Hz, dB)] from 0 Hz to Nyquist: the band gains joined in log frequency every sixth of an octave,
    held flat past the first and last band. firequalizer joins these points in straight lines and takes
    at most 100 of them."""
    pts = [(0.0, gains[0])]
    lo, hi = math.log2(BANDS[0]), math.log2(SR / 2.0)
    k = 0
    while lo + k * step <= hi:
        f = 2 ** (lo + k * step)
        x = (math.log2(f) - math.log2(BANDS[0])) * 3  # position in bands
        i = min(int(x), len(BANDS) - 2)
        t = min(1.0, x - i)
        pts.append((f, gains[i] + (gains[i + 1] - gains[i]) * t))
        k += 1
    return pts


def eq_filter(gains, n):
    """The ffmpeg linear-phase EQ for those gains on n samples: the filter's EQ_LAG samples of delay cut
    off the front, so the voice stays in sync and keeps its length."""
    entries = ";".join("entry(%.2f,%.3f)" % fg for fg in eq_curve(gains))
    return ("firequalizer=gain_entry='%s':delay=%.9f,atrim=start_sample=%d:end_sample=%d,asetpts=PTS-STARTPTS,"
            "apad=whole_len=%d" % (entries, EQ_LAG / float(SR), EQ_LAG, EQ_LAG + n, n))


# ---------------------------------------------------------------- 2. small-room echo
# The room's noise is numpy's default_rng(seed).standard_normal, the generator MEDIUM was picked by ear
# on, rebuilt here in plain Python (SeedSequence, PCG64 and the 256-layer ziggurat), so the room is the
# same draw, sample for sample, with no numpy.

_M32, _M64, _M128 = 0xFFFFFFFF, (1 << 64) - 1, (1 << 128) - 1
_PCG_MULT = 0x2360ED051FC65DA44385DF649FCCF645
_ZIG_R = 3.6541528853610087963519472518
_ZIG_INV_R = 0.27366123732975827203338247596


def _seed_words(seed):
    """numpy's SeedSequence(seed).generate_state(4, uint64) for a seed under 2**32."""
    hc = [0x43b0d7e5]

    def hashmix(v):
        v = (v ^ hc[0]) & _M32
        hc[0] = (hc[0] * 0x931e8875) & _M32
        v = (v * hc[0]) & _M32
        return v ^ (v >> 16)

    def mix(x, y):
        r = (0xca01f9dd * x - 0x4973f715 * y) & _M32
        return r ^ (r >> 16)
    pool = [hashmix(seed if i == 0 else 0) for i in range(4)]
    for a in range(4):
        for b in range(4):
            if a != b:
                pool[b] = mix(pool[b], hashmix(pool[a]))
    out, h = [], 0x8b51f9dd
    for i in range(8):
        v = (pool[i % 4] ^ h) & _M32
        h = (h * 0x58f38ded) & _M32
        v = (v * h) & _M32
        out.append(v ^ (v >> 16))
    return [out[2 * i] | (out[2 * i + 1] << 32) for i in range(4)]


class _PCG64:
    """numpy's PCG64 bit generator."""

    def __init__(self, seed):
        w = _seed_words(seed)
        self.inc = ((((w[2] << 64) | w[3]) << 1) | 1) & _M128
        self.state = (self.inc + ((w[0] << 64) | w[1])) & _M128
        self.state = (self.state * _PCG_MULT + self.inc) & _M128

    def next64(self):
        self.state = s = (self.state * _PCG_MULT + self.inc) & _M128
        v, rot = ((s >> 64) ^ s) & _M64, s >> 122
        return ((v >> rot) | (v << ((-rot) & 63))) & _M64

    def next_double(self):
        return (self.next64() >> 11) * (1.0 / 9007199254740992.0)


def _zig_tables():
    f = lambda x: math.exp(-0.5 * x * x)  # noqa: E731
    vn = _ZIG_R * f(_ZIG_R) + math.sqrt(math.pi / 2) * math.erfc(_ZIG_R / math.sqrt(2))
    m1 = 2.0 ** 52
    ki, wi, fi = [0] * 256, [0.0] * 256, [0.0] * 256
    dn = tn = _ZIG_R
    q = vn / f(dn)
    ki[0], wi[0], wi[255], fi[0], fi[255] = int(dn / q * m1), q / m1, dn / m1, 1.0, f(dn)
    for i in range(254, 0, -1):
        dn = math.sqrt(-2.0 * math.log(vn / dn + f(dn)))
        ki[i + 1], tn, fi[i], wi[i] = int(dn / tn * m1), dn, f(dn), dn / m1
    return ki, wi, fi


def _normals(seed, n):
    """numpy's default_rng(seed).standard_normal(n)."""
    rng, (ki, wi, fi), out = _PCG64(seed), _zig_tables(), []
    while len(out) < n:
        r = rng.next64()
        idx, r = r & 0xff, r >> 8
        rabs = (r >> 1) & 0x000fffffffffffff
        x = -rabs * wi[idx] if r & 1 else rabs * wi[idx]
        if rabs < ki[idx]:
            out.append(x)
        elif idx == 0:
            while True:
                xx = -_ZIG_INV_R * math.log1p(-rng.next_double())
                if -2 * math.log1p(-rng.next_double()) > xx * xx:
                    out.append(-(_ZIG_R + xx) if (rabs >> 8) & 1 else _ZIG_R + xx)
                    break
        elif (fi[idx - 1] - fi[idx]) * rng.next_double() + fi[idx] < math.exp(-0.5 * x * x):
            out.append(x)
    return out


def room_ir(rt60=0.35, length_s=0.5, predelay_ms=8, seed=7):
    """A small room's impulse response: decaying noise, highs absorbed, a short pre-delay, unit energy."""
    n = int(length_s * SR)
    tail = [g * math.exp(-6.91 * (i / SR) / rt60) for i, g in enumerate(_normals(seed, n))]
    run, smooth = 0.0, []                               # an 8-sample moving average: walls absorb highs
    padded = [0.0] * 4 + tail + [0.0] * 3
    for i in range(len(padded)):
        run += padded[i] - (padded[i - 8] if i >= 8 else 0.0)
        if i >= 7:
            smooth.append(run / 8)
    for i in range(int(predelay_ms * SR / 1000)):
        smooth[i] = 0.0
    norm = math.sqrt(math.fsum(v * v for v in smooth)) or 1.0
    return array.array("f", [v / norm for v in smooth])


def _ms(x):
    return math.fsum(v * v for v in x) / max(len(x), 1)


# ---------------------------------------------------------------- the match

def match(room_path, dry_path, out_path, wet=MEDIUM["wet"], rt60=MEDIUM["rt60"]):
    """Place the dry voice in the room audio's room and write it to out_path. Returns its seconds."""
    room, dry = load(room_path), load(dry_path)
    if not len(room) or not len(dry):
        raise ValueError("no audio read from %s" % (room_path if not len(room) else dry_path))
    n = len(dry)
    eq = eq_filter(tone_gains(room, dry), n)
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        ir_path = os.path.join(d, "ir.f32")
        with open(ir_path, "wb") as fh:
            fh.write(_bytes(room_ir(rt60)))
        raw = ["-f", "f32le", "-ar", str(SR), "-ac", "1"]
        # the voice through the EQ, beside the same voice through the room: two channels, measured below
        graph = ("[0:a]%s,asplit=2[y][yr];[yr][1:a]afir[w0];[w0]apad=whole_len=%d,atrim=end_sample=%d[w];"
                 "[y][w]amerge=inputs=2[out]" % (eq, n, n))
        both = _f32(_run(["ffmpeg", "-loglevel", "error"] + raw + ["-i", "-"] + raw + ["-i", ir_path,
                          "-filter_complex", graph, "-map", "[out]", "-f", "f32le", "-"], _bytes(dry)).stdout)
        y, w = both[0::2], both[1::2]
        a = math.sqrt(1 - wet) if wet > 0 else 1.0
        b = math.sqrt(wet) * math.sqrt(_ms(y) / (_ms(w) + 1e-12)) if wet > 0 else 0.0
        level = speech_rms(room) / speech_rms(array.array("f", [a * p + b * q for p, q in zip(y, w)]))
        _run(["ffmpeg", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-",
              "-af", "pan=mono|c0=%.9f*c0+%.9f*c1,atrim=end_sample=%d" % (a * level, b * level, n),
              "-c:a", "pcm_s16le", str(out_path)], _bytes(both))
    return n / float(SR)


# ---------------------------------------------------------------- self-test

def _tone(path, expr, secs):
    _run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
          "aevalsrc='%s':s=%d:d=%s" % (expr, SR, secs), "-c:a", "pcm_s16le", str(path)])


def self_test():
    import tempfile
    fails = []
    with tempfile.TemporaryDirectory() as d:
        talk = "gt(sin(2*PI*1.5*t),0)"  # talk, pause, talk: a third of a second each
        rp, dp, op = (os.path.join(d, n) for n in ("room.wav", "dry.wav", "out.wav"))
        _tone(rp, "0.3*%s*sin(2*PI*220*t)+0.01*(2*random(0)-1)" % talk, 3.0)
        _tone(dp, "0.6*%s*sin(2*PI*180*t)" % talk, 2.5)
        dry = load(dp)
        secs = match(rp, dp, op)
        out = load(op)
        if abs(len(out) - len(dry)) > SR * 0.01:
            fails.append("the output is not the dry voice's length")
        if abs(secs - 2.5) > 0.01:
            fails.append("match() did not report the dry voice's length")
        ratio = speech_rms(out) / speech_rms(load(rp))
        if not 0.7 < ratio < 1.4:
            fails.append("the speech level is not matched to the room audio (ratio %.2f)" % ratio)
        quiet = out[int(0.60 * SR): int(0.65 * SR)]  # late in the first pause (0.33 to 0.67 s), past the echo
        if math.sqrt(_ms(quiet)) > 2e-3:
            fails.append("the pauses are not silent: room tone or noise is laid under them")
        early = out[int(0.34 * SR): int(0.39 * SR)]  # just after the first phrase: the echo is there
        if math.sqrt(_ms(early)) < 1e-3:
            fails.append("no echo follows the speech")
        # the tone match: a voice brighter than the room comes out with the room's balance
        bp, bo = os.path.join(d, "bright.wav"), os.path.join(d, "bright_out.wav")
        _tone(rp, "0.2*%s*(sin(2*PI*300*t)+0.25*sin(2*PI*3000*t))" % talk, 3.0)
        _tone(bp, "0.2*%s*(sin(2*PI*300*t)+sin(2*PI*3000*t))" % talk, 2.5)
        match(rp, bp, bo, wet=0)
        lv = dict(zip(BANDS, band_levels(load(bo))))
        lo, hi = min(BANDS, key=lambda f: abs(f - 300)), min(BANDS, key=lambda f: abs(f - 3000))
        if not 6 < lv[lo] - lv[hi] < 18:
            fails.append("the tone match leaves 3 kHz %.1f dB under 300 Hz, not about 12" % (lv[lo] - lv[hi]))
    if MEDIUM != {"wet": 0.18, "rt60": 0.35}:
        fails.append("MEDIUM is not the agreed medium room setting")
    ir = room_ir()
    if abs(math.fsum(v * v for v in ir) - 1) > 1e-3 or any(ir[: int(0.008 * SR)]) or len(ir) != SR // 2:
        fails.append("room_ir() is not a unit-energy half-second response with its pre-delay")
    g = _normals(7, SR // 2)  # numpy's default_rng(7).standard_normal(22050): first, second and last
    if max(abs(a - b) for a, b in zip((g[0], g[1], g[-1]),
                                      (0.0012301533574825742, 0.2987455375084699, -1.0297414089977948))) > 1e-12:
        fails.append("the room's noise is not numpy's default_rng(7) draw")
    for f in fails:
        print("roommatch.py self-test: %s" % f)
    print("roommatch.py self-test: %s" % ("pass" if not fails else "fail"))
    return 0 if not fails else 1


def main(argv):
    if argv[:1] == ["--self-test"]:
        return self_test()
    if argv[:1] in (["-h"], ["--help"]) or len(argv) != 3:
        print(__doc__)
        return 0 if argv[:1] in (["-h"], ["--help"]) else 2
    try:
        match(argv[0], argv[1], argv[2])
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        print("roommatch.py: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
