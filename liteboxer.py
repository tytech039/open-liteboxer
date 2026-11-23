#!/usr/bin/env python3
"""
liteboxer.py -- independent controller for a Liteboxer (gen2) floor stand over BLE.

Protocol recovered from the official Android app v2.5; see PROTOCOL.md.

Requires:  pip3 install bleak

Subcommands:
    scan                      find the machine
    info                      connect, dump GATT, read battery/version  (read-only, safe)
    monitor                   subscribe to notifications and log raw frames (no writes)
    play                      enter play mode + keepalive + log hits   (Ctrl-C to idle out)
    idle                      force the machine back to idle
    target <pad> [pad ...]    enter play mode and light the given pads once
    freestyle [--bpm N]       random targets at a tempo
    track <file.json>         stream a choreography file
    selftest                  offline check of the byte encoders (no hardware)

Pads: 0=Top 1=TopLeft 2=TopRight 3=BottomLeft 4=BottomRight 5=Bottom
      (6=Duck and 7=Uppercut are cues, not physical targets)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import struct
import sys
import time

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("bleak is not installed.  Run:  pip3 install bleak")

# ---------------------------------------------------------------- UUIDs

SVC_LB = "43abf632-6ad7-4cd0-a6b8-000bebb5faa6"

CTRL_PT = "a5887ce9-160b-4238-add6-3757f64bfcfd"  # control point
PLAYBACK_STATUS = "29eda14a-f221-4a9b-bf83-43da05d4f3b5"  # play/stop
ANIMATION_SPEED = "09b4041f-0a90-4401-9aba-b4623adc34b7"  # lane time + keepalive
DATA_LOAD_SEND = "8be06e5e-5cfd-4a4e-8d2b-e80f0b0689d6"  # notes -> machine
DATA_LOAD_RECV = "adb53c85-7a04-4c68-b309-c694a3df3876"  # notify: flow ctl + hits
TIME_SYNC_SEND = "16cc30c0-fab8-48e5-903e-87bd8a9ee104"
TIME_SYNC_RECV = "e587f298-e119-4fec-9d5e-16c728c093e6"
IDLE_SELECT = "a9dfc89d-7e5e-432b-b53a-737d81ced823"

BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"
FIRMWARE_REV = "00002a26-0000-1000-8000-00805f9b34fb"
SOFTWARE_REV = "00002a28-0000-1000-8000-00805f9b34fb"

# Nordic DFU -- writing here can brick the unit. Never used by this tool.
FORBIDDEN = {
    "9d47b456-2047-46fd-87db-a7362413dee6",  # FIRMWARE_CONTROL_POINT
    "bef225b3-9ade-4f64-b9c3-923b301e5e27",  # FIRMWARE_DATA
}

PAD_NAMES = {
    0: "Top", 1: "TopLeft", 2: "TopRight",
    3: "BottomLeft", 4: "BottomRight", 5: "Bottom",
    6: "Duck", 7: "Uppercut", 255: "BadData",
}
PHYSICAL_PADS = [0, 1, 2, 3, 4, 5]

KEEPALIVE_SECONDS = 15
# The app's HeadUnitValues.LaneTimeLength defaults to 1000 ms
# (LaneTimeFromDifficulty = 1000 - difficulty*100). ANIMATION_SPEED is a
# 2-byte characteristic on the hardware, so this is a uint16.
DEFAULT_LANE_TIME = 1000
MAX_LANE_TIME = 0xFFFF
# ResultsLoop in the app sends a packet every 100 ms.
STREAM_INTERVAL = 0.100


# ---------------------------------------------------------------- encoders


def note_bytes(index: int, time_ms: int, pad: int,
               saw_green: bool = False, force: int = 0,
               did_punch: bool = False) -> bytes:
    """A single 7-byte Note.  See PROTOCOL.md section 4."""
    if not 0 <= index <= 0xFFFF:
        raise ValueError(f"index {index} out of range")
    if not 0 <= time_ms <= 0xFFFFFF:
        raise ValueError(f"time_ms {time_ms} exceeds the 24-bit field (max 16777215)")
    if not 0 <= pad <= 0x7F:
        raise ValueError(f"pad {pad} out of range")
    if not 0 <= force <= 0x7F:
        raise ValueError(f"force {force} out of range")
    return bytes([
        index & 0xFF,
        (index >> 8) & 0xFF,
        time_ms & 0xFF,
        (time_ms >> 8) & 0xFF,
        (time_ms >> 16) & 0xFF,
        (pad & 0x7F) | (0x80 if saw_green else 0x00),
        (force & 0x7F) | (0x80 if did_punch else 0x00),
    ])


def parse_note(buf: bytes, off: int) -> dict:
    """Inverse of note_bytes()."""
    idx = buf[off] | (buf[off + 1] << 8)
    t = buf[off + 2] | (buf[off + 3] << 8) | (buf[off + 4] << 16)
    pad_b, force_b = buf[off + 5], buf[off + 6]
    return {
        "index": idx,
        "time_ms": t,
        "pad": pad_b & 0x7F,
        "pad_name": PAD_NAMES.get(pad_b & 0x7F, "?"),
        "saw_green": bool(pad_b & 0x80),
        "force": force_b & 0x7F,
        "did_punch": bool(force_b & 0x80),
    }


def note_packet(results_count: int, highest_index: int,
                entries: list[bytes]) -> bytes:
    """A 19-byte NotePacket.  See PROTOCOL.md section 5.

    results_count goes in the field the app's NotePacket calls
    LowestMissingIndex. Outbound, ResultsLoop fills it with
    results.NoteCount -- the number of result notes received so far. It is the
    acknowledgement the machine waits on: until it increments, the machine
    re-sends the same result over and over."""
    if len(entries) > 2:
        raise ValueError("a NotePacket carries at most 2 notes")
    buf = bytearray(19)
    struct.pack_into("<HH", buf, 0, results_count & 0xFFFF, highest_index & 0xFFFF)
    buf[4] = len(entries)
    for i, e in enumerate(entries):
        buf[5 + i * 7 : 12 + i * 7] = e
    return bytes(buf)


def parse_note_packet(buf: bytes) -> dict:
    """Parse an inbound DATA_LOAD_RECEIVE frame."""
    if len(buf) < 5:
        return {"raw": buf.hex(), "error": "short frame"}
    lowest, highest = struct.unpack_from("<HH", buf, 0)
    count = buf[4] & 0x0F
    # Inbound, this first field is the machine's next-needed note index;
    # outbound the same slot carries our result-count ACK.
    out = {"lowest_missing": lowest, "highest_index": highest, "count": count,
           "notes": [], "raw": buf.hex()}
    for i in range(min(count, 2)):
        off = 5 + i * 7
        if len(buf) >= off + 7:
            out["notes"].append(parse_note(buf, off))
    return out



# ---------------------------------------------------------------- scoring

# Timing windows, measured from the note's scheduled time. The machine reports
# the real punch moment (verified: deltas spread -155..+18 ms against a known
# track), so these are meaningful rather than cosmetic.
WINDOWS = [(60, "PERFECT", 100), (120, "GREAT", 70), (250, "GOOD", 40)]
LATE_POINTS = 10
MATCH_WINDOW = 500          # ms either side to associate a hit with a target


class Scorer:
    """Matches reported hits back to scheduled targets and keeps a score."""

    def __init__(self, notes: list[dict]):
        # notes: [{time_ms, pad}, ...] as loaded, in play order
        self.targets = [{"time_ms": int(n["time_ms"]), "pad": int(n["pad"]),
                         "hit": None} for n in sorted(notes, key=lambda n: n["time_ms"])]
        self.score = 0
        self.combo = 0
        self.best_combo = 0
        self.counts = {"PERFECT": 0, "GREAT": 0, "GOOD": 0, "LATE": 0}
        self.deltas: list[int] = []
        self.forces: list[int] = []
        self.unmatched = 0

    @staticmethod
    def _rate(delta_ms: int):
        for limit, name, pts in WINDOWS:
            if abs(delta_ms) <= limit:
                return name, pts
        return "LATE", LATE_POINTS

    def multiplier(self) -> float:
        return min(2.0, 1.0 + (self.combo // 10) * 0.1)

    def register(self, pad: int, t_ms: int, force: int):
        """Score one reported hit. Returns (rating, points, delta) or None."""
        best, bd = None, MATCH_WINDOW + 1
        for tgt in self.targets:
            if tgt["pad"] != pad or tgt["hit"] is not None:
                continue
            d = abs(tgt["time_ms"] - t_ms)
            if d < bd:
                best, bd = tgt, d
        if best is None:
            self.unmatched += 1
            return None

        delta = t_ms - best["time_ms"]
        rating, pts = self._rate(delta)
        best["hit"] = {"delta": delta, "force": force, "rating": rating}

        self.combo += 1
        self.best_combo = max(self.best_combo, self.combo)
        gained = int(pts * self.multiplier())
        self.score += gained
        self.counts[rating] += 1
        self.deltas.append(delta)
        self.forces.append(force)
        return rating, gained, delta

    def report(self) -> str:
        total = len(self.targets)
        hits = sum(1 for t in self.targets if t["hit"])
        misses = total - hits
        acc = hits / total * 100 if total else 0.0
        out = ["", "=" * 46, f"{'SCORE':<22}{self.score:>24,}", "=" * 46,
               f"  targets          {total}",
               f"  hit              {hits}  ({acc:.0f}%)",
               f"  missed           {misses}"]
        for k in ("PERFECT", "GREAT", "GOOD", "LATE"):
            out.append(f"  {k.lower():<16} {self.counts[k]}")
        out.append(f"  best combo       {self.best_combo}")
        if self.forces:
            out.append(f"  force            avg {sum(self.forces)/len(self.forces):.1f}  "
                       f"max {max(self.forces)}")
        if self.deltas:
            med = sorted(self.deltas)[len(self.deltas) // 2]
            spread = sorted(abs(d) for d in self.deltas)[len(self.deltas) // 2]
            out.append(f"  timing           median {med:+d}ms, typical error {spread}ms")
            if abs(med) >= 40:
                direction = "early" if med < 0 else "late"
                out.append(f"                   consistently {abs(med)}ms {direction} - "
                           f"try --lead {'-' if med < 0 else '+'}{abs(med)} from current")
        if self.unmatched:
            out.append(f"  unmatched hits   {self.unmatched}  (no scheduled target nearby)")
        out.append("=" * 46)
        return "\n".join(out)


# ---------------------------------------------------------------- client


class Liteboxer:
    def __init__(self, client: BleakClient, verbose: bool = True,
                 trace: bool = False):
        self.c = client
        self.verbose = verbose
        self.trace = trace          # log every raw frame, not just hits
        # Pushing unsolicited 12-byte sync messages stopped the machine
        # replying at all in testing, so this is opt-in (--time-sync).
        self.send_time_sync = False
        self.hits: list[dict] = []
        self._seen_hits: set = set()
        self.notes: list[bytes] = []       # the track being streamed, by index
        self.highest_index = 0
        self.next_send_index = 0
        self.results_count = 0
        self._ka_task: asyncio.Task | None = None
        self.lane_time = DEFAULT_LANE_TIME
        self.scorer: Scorer | None = None
        self.last_device_ts = b"\x00\x00\x00\x00"
        self.last_device_ts_recv = 0
        self.play_start: float | None = None

    def log(self, *a):
        if self.verbose:
            print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)

    # -- raw writes, with a hard guard on the DFU characteristics ---------

    async def write(self, char: str, data: bytes, response: bool = True):
        if char.lower() in FORBIDDEN:
            raise RuntimeError(f"refusing to write to DFU characteristic {char}")
        await self.c.write_gatt_char(char, data, response=response)
        if self.trace or char != DATA_LOAD_SEND:
            self.log(f"  -> {char[:8]}  {data.hex()}")

    # -- notifications ---------------------------------------------------

    def _on_data_load(self, _sender, data: bytearray):
        pkt = parse_note_packet(bytes(data))
        if self.trace:
            self.log(f"  <- DATA_LOAD  {pkt}")

        # Mirrors LiteboxerHeadUnitBLE.OnReceiveResults exactly:
        #   if (packet.TwoEntries.HasValue) { QueueAdd(EntryA); if two: QueueAdd(EntryB); }
        #   nextSendIndex = packet.LowestMissingIndex;
        count = pkt.get("count", 0)
        if count:
            for n in pkt.get("notes", []):
                # Every result note counts toward the ACK, punch or not --
                # NoteCollection.NoteCount does not filter on DidPunch.
                self.results_count += 1
                if n["did_punch"]:
                    self.hits.append(n)
                    line = (f"    HIT  {n['pad_name']:<12} force={n['force']:<4} "
                            f"t={n['time_ms']}ms")
                    if self.scorer:
                        res = self.scorer.register(n["pad"], n["time_ms"], n["force"])
                        if res:
                            rating, pts, delta = res
                            line += (f"   {rating:<8} {delta:+5}ms  +{pts}"
                                     f"   x{self.scorer.combo}")
                    print(line, flush=True)

        lm = pkt.get("lowest_missing")
        if isinstance(lm, int):
            self.next_send_index = lm

    def _on_time_sync(self, _sender, data: bytearray):
        """The machine sends a 4-byte device timestamp; we keep it (and when we
        got it) to build the 12-byte reply."""
        raw = bytes(data)
        self.last_device_ts = raw[:4].ljust(4, b"\x00")
        self.last_device_ts_recv = self._now_ms()
        if self.trace:
            self.log(f"  <- TIME_SYNC  {raw.hex()}")

    def _now_ms(self) -> int:
        return int(time.monotonic() * 1000) & 0xFFFFFFFF

    def playback_ms(self) -> int:
        """Milliseconds since play started -- our stand-in for the app's
        getPlaybackTime(). For song mode this should be the audio position."""
        if self.play_start is None:
            return 0
        return int((time.monotonic() - self.play_start) * 1000) & 0xFFFFFFFF

    def sync_message(self) -> bytes:
        """The app's SyncTimes.GenerateSyncMessage(): 12 bytes --
        device timestamp | when we received it | current playback position."""
        return (self.last_device_ts
                + struct.pack("<I", self.last_device_ts_recv)
                + struct.pack("<I", self.playback_ms()))

    async def send_sync(self):
        try:
            await self.write(TIME_SYNC_SEND, self.sync_message(), response=False)
        except Exception as e:
            if self.trace:
                self.log(f"sync write failed: {e}")

    async def subscribe_all(self):
        """Subscribe to every notify/indicate characteristic, to find which
        channel carries hit events. Subscribing writes only to the CCCD, never
        to the characteristic value, so this is safe even on the DFU chars."""
        names = {
            DATA_LOAD_RECV: "DATA_LOAD_RECEIVE",
            TIME_SYNC_RECV: "TIME_SYNC_RECEIVE",
            CTRL_PT: "LB_CONTROL_PT",
            "73d02279-5a6d-43b6-8c74-3e079d40ee9a": "UNNAMED-73D0",
            "9d47b456-2047-46fd-87db-a7362413dee6": "FIRMWARE_CTRL",
        }
        for s in self.c.services:
            for ch in s.characteristics:
                if not ({"notify", "indicate"} & set(ch.properties)):
                    continue
                label = names.get(ch.uuid.lower(), ch.uuid[:8])

                def handler(sender, data, label=label):
                    raw = bytes(data)
                    print(f"[{time.strftime('%H:%M:%S')}]  <= {label:<20} "
                          f"len={len(raw):<3} {raw.hex()}", flush=True)
                    # Still run the real note-stream logic, otherwise the
                    # pull-based feed stalls after the primed notes.
                    if label == "DATA_LOAD_RECEIVE":
                        self._on_data_load(sender, data)

                try:
                    await self.c.start_notify(ch.uuid, handler)
                    self.log(f"subscribed {label}")
                except Exception as e:
                    self.log(f"could not subscribe {label}: {e}")

    async def subscribe(self):
        await self.c.start_notify(DATA_LOAD_RECV, self._on_data_load)
        self.log("subscribed DATA_LOAD_RECEIVE")
        try:
            await self.c.start_notify(TIME_SYNC_RECV, self._on_time_sync)
            self.log("subscribed TIME_SYNC_RECEIVE")
        except Exception as e:
            self.log(f"(TIME_SYNC_RECEIVE subscribe failed: {e})")

    # -- commands --------------------------------------------------------

    async def set_lane_time(self, ms: int | None = None):
        ms = self.lane_time if ms is None else ms
        if not 0 <= ms <= MAX_LANE_TIME:
            raise ValueError(f"lane time {ms} out of range for a uint16")
        self.lane_time = ms
        await self.write(ANIMATION_SPEED, struct.pack("<H", ms), response=True)

    async def enter_playmode(self, all_green: bool = True):
        self.log(f"EnterPlaymode(allGreen={all_green})")
        await self.write(CTRL_PT, bytes([0x04, 0x01 if all_green else 0x00, 0x02]))
        await self.write(PLAYBACK_STATUS, bytes([0x01]))
        self.play_start = time.monotonic()

    async def go_idle(self):
        self.log("Idle")
        try:
            await self.write(PLAYBACK_STATUS, bytes([0x00]))
            await self.write(CTRL_PT, bytes([0x08, 0x00]))
        except Exception as e:
            self.log(f"(idle failed: {e})")

    async def begin_track_load(self, flag: int = 0x00):
        self.log("Begin track load")
        await self.write(CTRL_PT, bytes([0x05, flag]))

    async def keepalive_loop(self):
        """The app re-sends lane time every 15 s; without it the session lapses."""
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_SECONDS)
                self.log("keepalive")
                await self.set_lane_time()
        except asyncio.CancelledError:
            pass

    def start_keepalive(self):
        self._ka_task = asyncio.create_task(self.keepalive_loop())

    async def stop_keepalive(self):
        if self._ka_task:
            self._ka_task.cancel()
            try:
                await self._ka_task
            except asyncio.CancelledError:
                pass
            self._ka_task = None

    # -- note streaming --------------------------------------------------

    def load_track(self, notes: list[dict]):
        """notes: [{time_ms, pad}, ...]  -- sorted and indexed for streaming."""
        notes = sorted(notes, key=lambda n: n["time_ms"])
        self.notes = [
            note_bytes(i, int(n["time_ms"]), int(n["pad"]),
                       saw_green=bool(n.get("saw_green", False)))
            for i, n in enumerate(notes)
        ]
        self.highest_index = max(0, len(self.notes) - 1)
        self.next_send_index = 0
        self.results_count = 0
        self.scorer = Scorer(notes)
        self.log(f"track loaded: {len(self.notes)} notes, "
                 f"last at {notes[-1]['time_ms'] if notes else 0} ms")

    async def stream_loop(self, duration: float):
        """Mirror the app's ResultsLoop: push a packet every 100 ms, two notes
        at a time, and keep pushing empty packets once the notes run out --
        that is what keeps hit results flowing back."""
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            entries: list[bytes] = []
            if self.next_send_index < len(self.notes):
                entries.append(self.notes[self.next_send_index])
                if self.next_send_index + 1 < len(self.notes):
                    entries.append(self.notes[self.next_send_index + 1])
            pkt = note_packet(self.results_count, self.highest_index, entries)
            try:
                await self.write(DATA_LOAD_SEND, pkt, response=False)
            except Exception as e:
                self.log(f"stream write failed: {e}")
            self.next_send_index += max(1, len(entries))
            if self.send_time_sync:
                await self.send_sync()
            await asyncio.sleep(STREAM_INTERVAL)


# ---------------------------------------------------------------- helpers


async def find_device(timeout: float = 15.0, address: str | None = None):
    if address:
        print(f"connecting directly to {address}")
        return address
    print(f"scanning {timeout:.0f}s for 'Litebox' ...")
    d = await BleakScanner.find_device_by_filter(
        lambda dev, adv: (dev.name or "").lower().startswith("litebox"),
        timeout=timeout)
    if d is None:
        sys.exit("Liteboxer not found. Is it powered on and not connected to the phone app?")
    print(f"found {d.name} at {d.address}")
    return d


# ---------------------------------------------------------------- commands


async def cmd_scan(args):
    print("scanning 15s ...")
    found = await BleakScanner.discover(timeout=15.0, return_adv=True)
    for addr, (d, adv) in sorted(found.items(), key=lambda kv: -(kv[1][1].rssi or -999)):
        mark = "   <<< LITEBOXER" if (d.name or "").lower().startswith("litebox") else ""
        print(f"  rssi={adv.rssi:>4}  {d.name!r:<30} {addr}{mark}")


async def cmd_info(args):
    dev = await find_device(address=args.address)
    async with BleakClient(dev) as c:
        print(f"connected: {c.address}\n")
        for s in c.services:
            tag = "  <-- Liteboxer service" if s.uuid.lower() == SVC_LB else ""
            print(f"service {s.uuid}{tag}")
            for ch in s.characteristics:
                warn = "   *** DFU - DO NOT WRITE" if ch.uuid.lower() in FORBIDDEN else ""
                print(f"    {ch.uuid}  {','.join(ch.properties)}{warn}")
        print()
        for name, u in (("battery", BATTERY_LEVEL),
                        ("firmware rev", FIRMWARE_REV),
                        ("software rev", SOFTWARE_REV)):
            try:
                v = await c.read_gatt_char(u)
                pretty = v.decode(errors="replace") if name != "battery" else f"{v[0]}%"
                print(f"{name:<14} {pretty}")
            except Exception as e:
                print(f"{name:<14} <unreadable: {e}>")


async def cmd_monitor(args):
    """Read-only: subscribe and watch. Makes no writes at all."""
    dev = await find_device(address=args.address)
    async with BleakClient(dev) as c:
        lb = Liteboxer(c)
        await lb.subscribe()
        print("listening (no writes). Punch a pad and watch. Ctrl-C to stop.")
        try:
            await asyncio.sleep(args.seconds)
        except asyncio.CancelledError:
            pass


async def _session(c, args, body):
    """Shared: subscribe, lane time, playmode, keepalive, body, then idle."""
    lb = Liteboxer(c, trace=getattr(args, 'trace', False))
    lb.send_time_sync = getattr(args, "time_sync", False)
    if getattr(args, "all_notify", False):
        await lb.subscribe_all()
    else:
        await lb.subscribe()
    await lb.set_lane_time(args.lane_time)
    await lb.enter_playmode(all_green=not args.no_green)
    lb.start_keepalive()
    try:
        await body(lb)
    finally:
        await lb.stop_keepalive()
        await lb.go_idle()
        if lb.scorer and lb.scorer.targets:
            print(lb.scorer.report())
        elif lb.hits:
            print(f"\n{len(lb.hits)} hits recorded")


async def cmd_play(args):
    dev = await find_device(address=args.address)
    async with BleakClient(dev) as c:
        async def body(lb):
            print(f"\nplay mode. watching for {args.seconds}s - punch the pads. Ctrl-C to stop.\n")
            await asyncio.sleep(args.seconds)
        await _session(c, args, body)


async def cmd_discover(args):
    """Enter play mode with every notification channel subscribed, to locate
    the hit-event channel. Punch the targets while this runs."""
    dev = await find_device(address=args.address)
    async with BleakClient(dev) as c:
        lb = Liteboxer(c)
        await lb.subscribe_all()
        await lb.set_lane_time(args.lane_time)
        await lb.enter_playmode(all_green=not args.no_green)
        lb.start_keepalive()
        print(f"\n>>> PUNCH THE TARGETS NOW - logging every channel for {args.seconds}s <<<\n")
        try:
            await asyncio.sleep(args.seconds)
        finally:
            await lb.stop_keepalive()
            await lb.go_idle()


async def cmd_idle(args):
    dev = await find_device(address=args.address)
    async with BleakClient(dev) as c:
        lb = Liteboxer(c)
        await lb.go_idle()
        print("sent idle")


async def cmd_target(args):
    dev = await find_device(address=args.address)
    async with BleakClient(dev) as c:
        async def body(lb):
            spacing = args.spacing
            notes = [{"time_ms": 1000 + i * spacing, "pad": p}
                     for i, p in enumerate(args.pads)]
            lb.load_track(notes)
            await lb.begin_track_load()
            total = (1000 + len(notes) * spacing + 3000) / 1000
            print(f"\nstreaming {len(notes)} targets over ~{total:.0f}s - punch them!")
            if args.hold:
                print(f"then holding the stream open {args.hold:.0f}s for results\n")
            else:
                print()
            await lb.stream_loop(total + args.hold)
        await _session(c, args, body)


async def cmd_freestyle(args):
    dev = await find_device(address=args.address)
    async with BleakClient(dev) as c:
        async def body(lb):
            interval = 60000 / args.bpm
            n = int(args.seconds * 1000 / interval)
            notes = [{"time_ms": int(1000 + i * interval),
                      "pad": random.choice(PHYSICAL_PADS)} for i in range(n)]
            lb.load_track(notes)
            await lb.begin_track_load()
            print(f"\nfreestyle: {n} targets at {args.bpm} BPM for {args.seconds}s\n")
            await lb.stream_loop(args.seconds + 2)
        await _session(c, args, body)


async def cmd_track(args):
    with open(args.file) as fh:
        doc = json.load(fh)
    notes = doc["notes"] if isinstance(doc, dict) else doc
    dev = await find_device(address=args.address)
    async with BleakClient(dev) as c:
        async def body(lb):
            lb.load_track(notes)
            await lb.begin_track_load()
            dur = max(n["time_ms"] for n in notes) / 1000 + 3
            print(f"\nstreaming {len(notes)} notes over ~{dur:.0f}s\n")
            await lb.stream_loop(dur)
        await _session(c, args, body)


def cmd_selftest(args):
    """Offline verification of the encoders against PROTOCOL.md."""
    ok = True

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {label}: {got.hex() if isinstance(got,bytes) else got}"
              + ("" if good else f"  (expected {want.hex() if isinstance(want,bytes) else want})"))

    print("note_bytes:")
    check("index=1, t=1000ms, pad=TopLeft",
          note_bytes(1, 1000, 1), bytes([0x01, 0x00, 0xE8, 0x03, 0x00, 0x01, 0x00]))
    check("saw_green sets bit7 of byte5",
          note_bytes(0, 0, 5, saw_green=True), bytes([0, 0, 0, 0, 0, 0x85, 0x00]))
    check("did_punch sets bit7 of byte6, force=100",
          note_bytes(0, 0, 0, force=100, did_punch=True), bytes([0, 0, 0, 0, 0, 0x00, 0xE4]))
    check("24-bit time 16777215",
          note_bytes(0, 0xFFFFFF, 0), bytes([0, 0, 0xFF, 0xFF, 0xFF, 0, 0]))

    print("round-trip:")
    n = parse_note(note_bytes(258, 123456, 4, saw_green=True, force=77, did_punch=True), 0)
    check("index", n["index"], 258)
    check("time_ms", n["time_ms"], 123456)
    check("pad", n["pad"], 4)
    check("saw_green", n["saw_green"], True)
    check("force", n["force"], 77)
    check("did_punch", n["did_punch"], True)

    print("note_packet:")
    p = note_packet(7, 99, [note_bytes(7, 500, 2), note_bytes(8, 1000, 3)])
    check("length", len(p), 19)
    check("results_count ack field", struct.unpack_from("<H", p, 0)[0], 7)
    check("highest_index", struct.unpack_from("<H", p, 2)[0], 99)
    check("entry count", p[4], 2)
    pp = parse_note_packet(p)
    check("parsed count", pp["count"], 2)
    check("parsed note0 index", pp["notes"][0]["index"], 7)
    check("parsed note1 pad", pp["notes"][1]["pad"], 3)

    print("\nguards:")
    for label, fn in (("time_ms overflow", lambda: note_bytes(0, 0x1000000, 0)),
                      ("pad overflow", lambda: note_bytes(0, 0, 200)),
                      ("3 entries", lambda: note_packet(0, 0, [b"x" * 7] * 3))):
        try:
            fn()
            print(f"  FAIL  {label}: no error raised")
            ok = False
        except ValueError:
            print(f"  PASS  {label}: rejected")

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(
        description="Independent BLE controller for a Liteboxer gen2 floor stand.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Pads: 0=Top 1=TopLeft 2=TopRight 3=BottomLeft 4=BottomRight 5=Bottom")
    ap.add_argument("--address", help="skip scanning; connect to this address")
    ap.add_argument("--lane-time", type=int, default=DEFAULT_LANE_TIME,
                    help=f"lane time in ms (default {DEFAULT_LANE_TIME})")
    ap.add_argument("--no-green", action="store_true",
                    help="pass allGreen=0 to EnterPlaymode")
    ap.add_argument("--time-sync", action="store_true",
                    help="experimental: push the 12-byte playback clock to TIME_SYNC_SEND")
    ap.add_argument("--trace", action="store_true",
                    help="log every raw frame sent and received")
    ap.add_argument("--all-notify", action="store_true",
                    help="subscribe to every notify/indicate channel and log raw frames")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan")
    sub.add_parser("info")
    sub.add_parser("idle")
    sub.add_parser("selftest")

    m = sub.add_parser("monitor")
    m.add_argument("--seconds", type=float, default=60)

    dc = sub.add_parser("discover")
    dc.add_argument("--seconds", type=float, default=40)

    p = sub.add_parser("play")
    p.add_argument("--seconds", type=float, default=60)

    t = sub.add_parser("target")
    t.add_argument("pads", type=int, nargs="+")
    t.add_argument("--spacing", type=int, default=1500, help="ms between targets")
    t.add_argument("--hold", type=float, default=0,
                   help="keep streaming this many extra seconds to collect results")

    f = sub.add_parser("freestyle")
    f.add_argument("--bpm", type=float, default=60)
    f.add_argument("--seconds", type=float, default=60)

    k = sub.add_parser("track")
    k.add_argument("file")

    args = ap.parse_args()

    if args.cmd == "selftest":
        sys.exit(cmd_selftest(args))

    fn = {
        "scan": cmd_scan, "info": cmd_info, "monitor": cmd_monitor,
        "play": cmd_play, "idle": cmd_idle, "target": cmd_target,
        "discover": cmd_discover,
        "freestyle": cmd_freestyle, "track": cmd_track,
    }[args.cmd]

    try:
        asyncio.run(fn(args))
    except KeyboardInterrupt:
        print("\ninterrupted")


if __name__ == "__main__":
    main()
