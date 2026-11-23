#!/usr/bin/env python3
"""
choreograph.py -- turn an audio file into a Liteboxer choreography JSON.

Beat-tracks the audio (spectral-flux onsets -> autocorrelation tempo -> phase
aligned beat grid), measures per-bar energy, and lays boxing combos onto the
beat grid, denser in the loud sections.

Requires ffmpeg on PATH, plus numpy and scipy.

    python3 tools/choreograph.py song.mp3 -o song.json
    python3 tools/choreograph.py song.mp3 --bpm 94 --lead 200

Pads: 0=Top 1=TopLeft 2=TopRight 3=BottomLeft 4=BottomRight 5=Bottom
Read as a boxer's target map: 1/2 = head-height jab/cross, 3/4 = body hooks,
0 = overhand/high, 5 = low.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import numpy as np
from scipy.signal import stft

SR = 22050
HOP = 512

# Combos as (beat offset within the bar, pad) so the rhythm is explicit and
# fills the whole bar instead of bunching at the downbeat.
# 1 = lead/jab, 2 = rear/cross, 3/4 = body hooks, 0 = overhand, 5 = low.
COMBOS_LOW = [
    [(0, 1), (2, 2)],                                   # jab ... cross
    [(0, 1), (2, 3)],                                   # jab ... body
    [(0, 2), (2, 1)],                                   # cross ... jab
    [(0, 3), (2, 4)],                                   # body ... body
]
COMBOS_MID = [
    [(0, 1), (0.5, 2), (2, 1)],                         # one-two ... jab
    [(0, 1), (1, 2), (2, 3)],                           # jab, cross, body
    [(0, 1), (0.5, 1), (1, 2), (3, 0)],                 # double jab, cross, overhand
    [(0, 1), (0.5, 2), (2, 3), (2.5, 4)],               # one-two, body-body
    [(0, 2), (1, 4), (2, 1), (3, 2)],                   # cross, hook, one-two
    [(0, 1), (1, 3), (2, 2), (3, 4)],
]
COMBOS_HIGH = [
    [(0, 1), (0.5, 2), (1, 3), (1.5, 4), (3, 0)],       # 1-2-3-4 then overhand
    [(0, 1), (0.5, 1), (1, 2), (2, 3), (2.5, 4), (3, 0)],
    [(0, 1), (0.5, 2), (1, 1), (1.5, 2), (2.5, 0)],     # four-punch burst, overhand
    [(0, 3), (0.5, 4), (1, 0), (2, 1), (2.5, 2)],       # body-body-high, one-two
    [(0, 1), (0.5, 2), (1.5, 3), (2, 4), (3, 5)],
    [(0, 2), (0.5, 1), (1, 2), (2, 4), (2.5, 3), (3.5, 0)],
]


# Played at the end of an 8-bar phrase: busier, and resolves on a big shot.
COMBOS_FILL = [
    [(0, 1), (0.5, 2), (1, 3), (1.5, 4), (2, 0), (3, 5)],
    [(0, 3), (0.5, 4), (1, 1), (1.5, 2), (2.5, 0)],
    [(0, 1), (0.5, 1), (1, 2), (1.5, 2), (2, 3), (3, 0)],
    [(0, 2), (1, 1), (1.5, 2), (2, 4), (3, 0)],
]


def onset_peaks(env: np.ndarray, fps: float) -> np.ndarray:
    """Times (s) of local maxima in the onset envelope, for snapping."""
    e = env
    idx = np.where((e[1:-1] >= e[:-2]) & (e[1:-1] > e[2:]) &
                   (e[1:-1] > e.mean()))[0] + 1
    return idx / fps


def snap(t: float, peaks: np.ndarray, tol: float) -> float:
    """Pull t onto the nearest real onset if one is within tol seconds."""
    if not len(peaks) or tol <= 0:
        return t
    i = int(np.searchsorted(peaks, t))
    best, bd = t, tol
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(peaks):
            d = abs(peaks[j] - t)
            if d < bd:
                best, bd = float(peaks[j]), d
    return best


def decode(path: str) -> np.ndarray:
    """Decode any audio file to mono float32 at SR via ffmpeg."""
    cmd = ["ffmpeg", "-v", "error", "-i", path,
           "-map", "a:0", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def onset_envelope(y: np.ndarray) -> np.ndarray:
    """Spectral flux: positive frame-to-frame change in the magnitude spectrum."""
    _, _, Z = stft(y, fs=SR, nperseg=2048, noverlap=2048 - HOP,
                   window="hann", boundary=None, padded=False)
    mag = np.abs(Z)
    # log-compress so quiet detail still registers
    mag = np.log1p(10.0 * mag)
    flux = np.diff(mag, axis=1)
    env = np.maximum(flux, 0).sum(axis=0)
    # subtract a local mean to flatten long-term loudness changes
    k = 16
    kern = np.ones(k) / k
    env = env - np.convolve(env, kern, mode="same")
    env = np.maximum(env, 0)
    return env / (env.max() or 1.0)


def estimate_tempo(env: np.ndarray, lo=60.0, hi=180.0) -> float:
    """Autocorrelate the onset envelope and pick the strongest plausible lag."""
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[len(e) - 1:]
    fps = SR / HOP
    lag_lo, lag_hi = int(fps * 60.0 / hi), int(fps * 60.0 / lo)
    window = ac[lag_lo:lag_hi]
    if not len(window):
        return 120.0
    # prefer lags with real support; weight mildly against very long lags
    lags = np.arange(lag_lo, lag_hi)
    score = window / (1.0 + 0.004 * lags)
    best = lags[int(np.argmax(score))]
    bpm = 60.0 * fps / best
    # fold into a danceable range
    while bpm < 70:
        bpm *= 2
    while bpm > 180:
        bpm /= 2
    return float(bpm)


def beat_phase(env: np.ndarray, bpm: float) -> float:
    """Find the grid offset (in frames) whose beats land on the most onset energy."""
    fps = SR / HOP
    period = 60.0 / bpm * fps
    best_off, best_val = 0.0, -1.0
    for off in np.arange(0, period, 0.5):
        idx = np.round(np.arange(off, len(env), period)).astype(int)
        idx = idx[idx < len(env)]
        if not len(idx):
            continue
        v = env[idx].mean()
        if v > best_val:
            best_val, best_off = v, off
    return best_off


def bar_energy(y: np.ndarray, beat_times: np.ndarray, beats_per_bar=4) -> np.ndarray:
    """RMS energy per bar, normalised 0..1."""
    out = []
    for i in range(0, len(beat_times), beats_per_bar):
        t0 = beat_times[i]
        t1 = beat_times[min(i + beats_per_bar, len(beat_times) - 1)]
        a, b = int(t0 * SR), int(max(t1 * SR, t0 * SR + 1))
        seg = y[a:b]
        out.append(float(np.sqrt((seg.astype(np.float64) ** 2).mean())) if len(seg) else 0.0)
    e = np.array(out)
    if e.max() > 0:
        e = e / e.max()
    return e


def build(beat_times, energies, lead_ms, min_gap_ms, beats_per_bar=4, seed=11,
          peaks=None, snap_tol=0.0, phrase_bars=8):
    """Lay combos onto the beat grid, intensity following bar energy."""
    rng = np.random.default_rng(seed)
    notes = []
    counters = {"low": 0, "mid": 0, "high": 0}

    # thresholds from the energy distribution, so it adapts to the mix
    t_lo, t_hi = np.quantile(energies, [0.35, 0.72])
    silence = t_lo * 0.45
    counters["fill"] = 0

    for bar_i, e in enumerate(energies):
        start = bar_i * beats_per_bar
        if start + 1 >= len(beat_times):
            break
        if e < silence:
            continue                                   # genuine silence: rest
        # last bar of an 8-bar phrase gets a fill, provided there is energy for it
        if phrase_bars and bar_i % phrase_bars == phrase_bars - 1 and e >= t_lo:
            tier, pool = "fill", COMBOS_FILL
        elif e < t_lo:
            tier, pool = "low", COMBOS_LOW
        elif e < t_hi:
            tier, pool = "mid", COMBOS_MID
        else:
            tier, pool = "high", COMBOS_HIGH

        # cycle the pool so it feels composed, with an occasional swap
        idx = counters[tier] % len(pool)
        if rng.random() < 0.25:
            idx = int(rng.integers(len(pool)))
        combo = pool[idx]
        counters[tier] += 1

        beat = beat_times[start + 1] - beat_times[start]
        for off, pad in combo:
            t = beat_times[start] + off * beat
            t = snap(t, peaks, snap_tol)
            notes.append({"time_ms": int(round(t * 1000)) - lead_ms, "pad": int(pad)})

    # clean up: in-range, sorted, and never two targets closer than min_gap
    notes = [n for n in notes if n["time_ms"] >= 0]
    notes.sort(key=lambda n: n["time_ms"])
    cleaned = []
    for n in notes:
        if cleaned and n["time_ms"] - cleaned[-1]["time_ms"] < min_gap_ms:
            continue
        cleaned.append(n)
    return cleaned


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio")
    ap.add_argument("-o", "--out", help="output .json (default: alongside input)")
    ap.add_argument("--name", help="choreography name")
    ap.add_argument("--bpm", type=float, help="override the detected tempo")
    ap.add_argument("--lead", type=int, default=180,
                    help="light each target this many ms before the beat (default 180)")
    ap.add_argument("--min-gap", type=int, default=150,
                    help="minimum ms between consecutive targets (default 150)")
    ap.add_argument("--half-time", choices=["auto", "on", "off"], default="auto",
                    help="punch on every other beat. Fast tracks need this: at "
                         "160 BPM a half-beat is 186 ms, far quicker than a punch "
                         "cycle. 'auto' engages above --half-time-above (default)")
    ap.add_argument("--half-time-above", type=float, default=140.0,
                    help="BPM above which 'auto' half-time engages (default 140)")
    ap.add_argument("--snap", type=int, default=90,
                    help="snap each target to a real onset within this many ms "
                         "(0 disables; default 90)")
    ap.add_argument("--phrase", type=int, default=8,
                    help="bars per phrase; the last bar gets a fill (0 disables)")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    print(f"decoding {args.audio} ...")
    y = decode(args.audio)
    dur = len(y) / SR
    print(f"  {dur:.1f}s  ({int(dur // 60)}m{dur % 60:04.1f}s)")

    env = onset_envelope(y)
    bpm = args.bpm or estimate_tempo(env)
    src = "given" if args.bpm else "detected"
    print(f"tempo ({src}): {bpm:.1f} BPM")

    fps = SR / HOP
    period = 60.0 / bpm * fps
    off = beat_phase(env, bpm)
    frames = np.arange(off, len(env), period)
    beat_times = frames / fps
    print(f"beat grid: {len(beat_times)} beats, first at {beat_times[0]:.3f}s")

    # Half-time: keep every other beat, so combo subdivisions land on intervals
    # a person can actually punch. The grid stays locked to the music.
    halve = args.half_time == "on" or (
        args.half_time == "auto" and bpm > args.half_time_above)
    if halve:
        beat_times = beat_times[::2]
        print(f"half-time: {bpm:.1f} -> {bpm/2:.1f} BPM "
              f"(beat {60000/bpm:.0f}ms -> {120000/bpm:.0f}ms), "
              f"{len(beat_times)} punch beats")
        bpm = bpm / 2.0

    energies = bar_energy(y, beat_times)
    print(f"bars: {len(energies)}")

    peaks = onset_peaks(env, fps)
    notes = build(beat_times, energies, args.lead, args.min_gap, seed=args.seed,
                  peaks=peaks, snap_tol=args.snap / 1000.0, phrase_bars=args.phrase)
    print(f"onset peaks available for snapping: {len(peaks)}")
    if not notes:
        sys.exit("no notes generated")

    out = args.out or (args.audio.rsplit(".", 1)[0] + ".json")
    name = args.name or args.audio.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    doc = {
        "name": name,
        "source_audio": args.audio.rsplit("/", 1)[-1],
        "bpm": round(bpm, 1),
        "half_time": bool(halve),
        "duration_ms": int(dur * 1000),
        "lead_ms": args.lead,
        "notes": notes,
    }
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=1)

    pads = np.bincount([n["pad"] for n in notes], minlength=6)
    names = ["Top", "TopLeft", "TopRight", "BottomLeft", "BottomRight", "Bottom"]
    print(f"\n{len(notes)} targets -> {out}")
    print(f"  last target at {notes[-1]['time_ms']/1000:.1f}s")
    print(f"  density {len(notes)/dur*60:.0f} targets/min")
    for i, c in enumerate(pads):
        print(f"  {names[i]:<12} {c:>4}")


if __name__ == "__main__":
    main()
