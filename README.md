# Liteboxer independent controller

Run your own Liteboxer (gen2 floor stand) without the official app — start a
session, light targets, read punches, and build custom light choreography for
your own music.

**Status: working on real hardware.** Handshake, target lighting, and punch
telemetry (pad + force + timing) are all verified. See
[`PROTOCOL.md`](PROTOCOL.md) for the full byte-level protocol.

| File | What it is |
|---|---|
| `PROTOCOL.md` | The reverse-engineered protocol, with confidence labels |
| `liteboxer.py` | Command-line controller / test tool (macOS, Linux, Windows) |
| `liteboxer.html` | Self-contained Web Bluetooth controller (Chrome / Edge) |
| `example-song.json` | A 60-second example choreography |
| `tools/choreograph.py` | Turns any audio file into a beat-matched choreography |
| `tools/unblob.py` | Extracts .NET assemblies from the Android app's `assemblies.blob` |
| `tools/ildasm.py` | Minimal CIL disassembler used to read the protocol out of the app |

> ⚠️ Two characteristics (`9D47B456`, `BEF225B3`) are the firmware-update path
> and are the one realistic way to brick the machine. Both tools **hard-block**
> writes to them. Don't poke them by hand either.

---

## Quick start — Mac / PC

```sh
pip3 install bleak
python3 liteboxer.py selftest      # offline sanity check, no hardware needed
python3 liteboxer.py scan          # confirm the machine is visible
python3 liteboxer.py info          # read-only GATT dump
```

Then make it do something. Turn the machine on first; it must not be connected
to the phone app at the same time.

```sh
# light all six targets and watch for punches
python3 liteboxer.py play --seconds 30

# light specific targets in order, and report the hits
python3 liteboxer.py target 0 1 2 3 4 5 --spacing 2000 --hold 6

# random targets at 50 per minute for a minute
python3 liteboxer.py freestyle --bpm 50 --seconds 60

# run a choreography file
python3 liteboxer.py track example-song.json

# if it ever gets stuck lit
python3 liteboxer.py idle
```

Pads are `0=Top 1=TopLeft 2=TopRight 3=BottomLeft 4=BottomRight 5=Bottom`.

Useful flags: `--trace` logs every frame both ways, `--lane-time N` sets how long
a target stays lit (ms, default 1000), `--address <uuid>` skips scanning.

## Quick start — browser

`liteboxer.html` is a self-contained Web Bluetooth controller: connect, play /
idle, a six-pad grid laid out like the machine, freestyle, and song mode with a
local audio file. Tap a pad to fire that target; hits appear in the log with
their force.

Web Bluetooth needs a **secure context**, so `file://` will not work — serve it
over localhost:

```sh
python3 -m http.server 8000
# then open http://localhost:8000/liteboxer.html in Chrome or Edge
```

Then **Connect** → pick `Litebox` → **Enter play mode**.

The page makes no external requests, and audio files you load never leave the
device.

---

## Making your own songs

A choreography file is just a list of `{time_ms, pad}`:

```json
{
  "name": "My track",
  "notes": [
    { "time_ms": 1000, "pad": 1 },
    { "time_ms": 1600, "pad": 2 },
    { "time_ms": 2200, "pad": 0 }
  ]
}
```

- `time_ms` — when to light it, measured from the start of the track
- `pad` — `0`–`5` as above
- `saw_green` — optional, defaults to false

A bare top-level array works too. Times get sorted automatically, so you can
append notes in any order. The hard ceiling is 16,777,215 ms (~4 h 39 m),
because the wire format stores time in 24 bits.

### Generating one from an audio file (recommended)

`tools/choreograph.py` beat-tracks the audio and writes a choreography matched
to it. Needs `ffmpeg` on PATH plus `numpy` and `scipy`.

```sh
python3 tools/choreograph.py "my-song.mp3" -o my-song.json
```

It prints what it found and what it made:

```
tempo (detected): 95.7 BPM
beat grid: 348 beats, first at 0.453s
293 targets -> my-song.json
  density 80 targets/min
```

How it works: spectral-flux onset detection → autocorrelation tempo estimate →
beat grid phase-aligned to the onsets → per-bar RMS energy. Each bar gets a
boxing combo whose intensity follows that bar's energy, so quiet intros stay
sparse and choruses fill out. Combos are written as *(beat offset, pad)* pairs,
so punches land on musical subdivisions rather than bunching on the downbeat.

Useful flags:

- `--bpm 94` — override the detected tempo if it locks on to half or double time
- `--lead 200` — light each target this many ms *before* the beat (default 180)
- `--min-gap 200` — never put two targets closer than this
- `--seed 42` — a different combo shuffle for the same song

Choreography generated from commercial music is tied to that specific
recording, so none is included here — generate your own from audio you own.
`example-song.json` is a synthetic demo with no source track.

### Generating one from a tempo

For a steady stream of targets, the tools will do it for you — `freestyle` on
the command line, or **Auto-generate from tempo** in the web page, which fills
the full length of the loaded audio.

To build one in Python, matching a known BPM:

```python
import json

bpm, bars = 100, 32
beat = 60000 / bpm
combos = [[1, 2], [1, 2, 0], [3, 4], [0, 5]]   # jab-cross, etc.

notes, t = [], 1000
for i in range(bars):
    for pad in combos[i % len(combos)]:
        notes.append({"time_ms": round(t), "pad": pad})
        t += beat
    t += beat                                   # a beat of rest

json.dump({"name": f"{bpm} BPM", "notes": notes},
          open("my-song.json", "w"), indent=1)
```

### Lining targets up with your music

Pick a target slightly *before* the beat you want punched — a punch takes time
to travel. Starting around 150–250 ms early feels about right; tune to taste
with `--lane-time`, which controls how long each target stays lit.

In the web page, the audio element and the target stream are started together,
so they stay aligned for the length of a normal song. The official app also
runs a clock-sync channel for tighter correction over long tracks
(`TIME_SYNC`, documented in `PROTOCOL.md` §6b) — it isn't needed here, because
each note carries an absolute timestamp and the machine schedules on its own
clock.

### Playlists

Run several files back to back:

```sh
for f in song1.json song2.json song3.json; do
    python3 liteboxer.py track "$f"
done
```

---

## How it works, briefly

The machine pulls rather than listens. After `[0x04, allGreen, 0x02]` on the
control point and `[0x01]` on playback status, you push a 19-byte frame to
`DATA_LOAD_SEND` every 100 ms carrying up to two 7-byte notes. The machine
notifies back on `DATA_LOAD_RECEIVE` with the next note index it wants, plus
results for notes it has retired — each with the pad, the force, and the actual
punch time.

The one non-obvious part: the first two bytes you send are **a running count of
results you've received**, not the index the machine asked for. It re-sends the
same result until that counter moves. Get this wrong and lights work fine but
punches never report — which is exactly what happened during development.

Re-send the lane time to `ANIMATION_SPEED` every 15 seconds as a keepalive.

## Troubleshooting

**Nothing is found when scanning.** Check Bluetooth is on (`system_profiler
SPBluetoothDataType | head` on a Mac). Make sure the phone app isn't holding the
connection — force-quit it, or turn the phone's Bluetooth off.

**Connects, but nothing lights.** You need the handshake, not just a connection:
`play` or **Enter play mode**. Writing to characteristics without it leaves the
machine in its attract loop.

**Targets light but punches never report.** The result-count ACK isn't
advancing — see above. `--trace` will show whether inbound frames have a
non-zero entry count.

**Machine stuck lit.** `python3 liteboxer.py idle`, or power-cycle it.

## Credits & scope

Protocol recovered from the official Android app v2.5, which ships as
unobfuscated Xamarin/.NET. For personal use with hardware you own. No Liteboxer
code, assets, or licensed music is redistributed here — only the protocol
description and clean-room tools.
