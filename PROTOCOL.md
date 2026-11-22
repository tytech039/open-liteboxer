# Liteboxer (gen2 floor stand) BLE Protocol

Recovered by decompiling the official Android app **v2.5** (`com.liteboxer.Liteboxer`).

The app is **Xamarin/.NET and completely unobfuscated**. All 200 managed assemblies were
extracted from `assemblies/assemblies.blob`; the protocol lives in:

- `Liteboxer.Services.HeadUnit.dll` — BLE transport, commands, UUID table
- `Liteboxer.Api.Models.dll` — `Note`, `Pad` wire types

Device advertises as **`Litebox`**. Internally the floor stand is the **"gen2"** head unit
(firmware files are named `lb-gen2-fw_bank{0}_{1}.bin`). A separate, smaller product
("**LB Go**" / **BX100**) shares the assembly but uses entirely different UUIDs — ignore those.

Confidence labels: **[C]** = read directly from decompiled code. **[I]** = inferred, not yet
verified against hardware.

---

## 1. GATT map

Primary service **`LB_SERVICE` = `43ABF632-6AD7-4CD0-A6B8-000BEBB5FAA6`** **[C]**

| Name | UUID | Role |
|---|---|---|
| `LB_CONTROL_PT` | `A5887CE9-160B-4238-ADD6-3757F64BFCFD` | Control point — mode changes |
| `PLAYBACK_STATUS` | `29EDA14A-F221-4A9B-BF83-43DA05D4F3B5` | Play / stop |
| `ANIMATION_SPEED` | `09B4041F-0A90-4401-9ABA-B4623ADC34B7` | Lane time **and keepalive** |
| `DATA_LOAD_SEND` | `8BE06E5E-5CFD-4A4E-8D2B-E80F0B0689D6` | Note stream → machine |
| `DATA_LOAD_RECEIVE` | `ADB53C85-7A04-4C68-B309-C694A3DF3876` | Notify: flow control + hit results |
| `TIME_SYNC_SEND` | `16CC30C0-FAB8-48E5-903E-87BD8A9EE104` | Playback clock sync |
| `TIME_SYNC_RECEIVE` | `E587F298-E119-4FEC-9D5E-16C728C093E6` | Notify: clock sync |
| `IDLE_SELECT` | `A9DFC89D-7E5E-432B-B53A-737D81CED823` | Idle/attract pattern select |
| `FIRMWARE_CONTROL_POINT` | `9D47B456-2047-46FD-87DB-A7362413DEE6` | ⚠️ DFU control (also version query) |
| `FIRMWARE_DATA` | `BEF225B3-9ADE-4F64-B9C3-923B301E5E27` | ⚠️ DFU payload |

Two further characteristics exist on the device but are **never referenced by
name anywhere in the app**: `73D02279-5A6D-43B6-8C74-3E079D40EE9A`
(read/write/indicate, reads `00000000`) and
`7AFE26C5-270C-4419-8C27-A1987A2D6772` (read/write, reads `0000`).

The app also knows the standard battery (`0000180F`/`00002A19`) and device-info
(`00002A26`/`00002A28`) characteristics, but **the gen2 floor stand does not
expose them** — it advertises only the one custom service. Those belong to the
LB Go. Verified by GATT enumeration.

### Observed characteristic sizes (read at idle)

| Characteristic | Bytes | Idle value |
|---|---|---|
| `LB_CONTROL_PT` | 20 | all zero |
| `PLAYBACK_STATUS` | 1 | `00` |
| `ANIMATION_SPEED` | 2 | `0000` |
| `IDLE_SELECT` | 1 | `00` |
| `73D02279` | 4 | `00000000` |
| `7AFE26C5` | 2 | `0000` |

> ⚠️ **Do not write to `FIRMWARE_CONTROL_POINT` or `FIRMWARE_DATA`.** These are the Nordic
> DFU path. Arbitrary writes there are the one realistic way to brick the unit.
> (Both were written `0101` during earlier blind-poking; no damage appears to have resulted,
> but don't repeat it.)

---

## 2. Enumerations

### `Pad` — target index **[C]**

| Value | Pad |
|---|---|
| 0 | Top |
| 1 | TopLeft |
| 2 | TopRight |
| 3 | BottomLeft |
| 4 | BottomRight |
| 5 | Bottom |
| 6 | Duck *(cue only, not a physical target)* |
| 7 | Uppercut *(cue only)* |
| 255 | BadData |

### `PuckColor` **[C]**

| Value | Colour |
|---|---|
| 0 | Off |
| 1 | LiteboxerGreen |
| 2 | LiteboxerOrange |
| 3 | LiteboxerBlue |
| 4 | Green |
| 5 | Red |
| 6 | Blue |
| 7 | White |
| 8 | Black |

---

## 3. Commands

All multi-byte integers are **little-endian**.

### Enter play mode — leaves the idle/attract loop **[C]**

```
LB_CONTROL_PT   <- [0x04, allGreen, 0x02]      allGreen = 0x01 or 0x00
PLAYBACK_STATUS <- [0x01]
```

From `LiteboxerHeadUnitBLE.EnterPlaymode(bool allGreen)`. **This is the handshake that was
missing** — blind writes without it leave the machine in its attract loop.

### Return to idle **[C]**

```
PLAYBACK_STATUS <- [0x00]
LB_CONTROL_PT   <- [0x08, 0x00]
```

### Menu mode **[C]**

```
LB_CONTROL_PT   <- [0x08, 0x01, <18 more bytes>]      // 20 bytes total
```

The trailing bytes carry per-puck colours (a loop over 6 pads). Exact packing **[I]**.

### Lane time / keepalive **[C]**

```
ANIMATION_SPEED <- uint16 LE lane time in ms      e.g. 1000 -> e8 03
```

`ANIMATION_SPEED` is a **2-byte** characteristic, so this is a **uint16**, not a
uint32. `HeadUnitValues.LaneTimeLength` defaults to **1000 ms**, and
`LaneTimeFromDifficulty` computes `1000 - difficulty*100`. **[C]**

`KeepAlive()` calls this **every 15 seconds** in a loop, retrying after 15 s on
failure. **[C, hardware-verified]**

### Begin track load **[C]**

```
LB_CONTROL_PT   <- [0x05, <1 byte>]
```

Then stream notes (below).

### Firmware version query **[C]**

```
FIRMWARE_CONTROL_POINT <- [0x04]
```

Read-only in intent, but on the DFU characteristic — **skip it**; the standard
`00002A26` / `00002A28` characteristics give version info safely.

### Known control-point opcodes

| Opcode | Meaning |
|---|---|
| `0x04` | Enter play mode |
| `0x05` | Begin track load |
| `0x08` | Mode select (`0x00` idle, `0x01` menu) |

### Control-point acknowledgement

`LB_CONTROL_PT` indicates a 20-byte ack after each write **[hardware-verified]**:

```
write 04 01 02  ->  ack 04 00 02 00 00 ...
write 08 00     ->  ack 08 01 02 00 00 ...
```

Byte 0 echoes the opcode. The meaning of bytes 1–2 is **[I]**.

---

## 4. Note — the light-choreography primitive

`Note.Bytes()` → **7 bytes** **[C]**

| Byte | Field |
|---|---|
| 0–1 | `Index` — uint16 LE, note's position in the track |
| 2–4 | `TimeMS` — uint**24** LE, milliseconds from track start |
| 5 | `Pad` in bits 0–6; **bit 7 = SawGreen** |
| 6 | `Force` in bits 0–6; **bit 7 = DidPunch** |

A uint24 timestamp gives a ceiling of 16,777,215 ms ≈ **4 h 39 m** per track.

## 5. NotePacket — the streaming frame

**19 bytes.** Same field offsets in both directions, but the first field means
different things each way — this is the crux of the protocol. **[C, hardware-verified]**

| Byte | Outbound (app → machine) | Inbound (machine → app) |
|---|---|---|
| 0–1 | **`resultsCount`** — number of result notes received so far (an ACK) | **next note index the machine needs** |
| 2–3 | highest note index in the track | machine's highest note index |
| 4 | entry count: `0`, `1`, or `2` | entry count (masked `& 0x0F`) |
| 5–11 | note to play (7-byte Note) | result note (`DidPunch`/`Force` filled in) |
| 12–18 | second note to play | second result note |

### The acknowledgement — the one thing that makes hits work

`ResultsLoop` builds each outbound frame as:

```csharp
new NotePacket(data, nextSendIndex, results.NoteCount)
//                                  ^ lands in bytes 0-1 on the wire
```

So bytes 0–1 outbound carry **`results.NoteCount`** — a running count of every
result note received (it does *not* filter on `DidPunch`). **The machine
re-sends the same result frame until that counter increments.** Echoing the
machine's own index back instead leaves it stuck repeating one hit forever —
confirmed on hardware, and the single most important detail here.

And `OnReceiveResults` is, in full:

```csharp
var packet = new NotePacket(args.Data);
if (packet.TwoEntries.HasValue) {          // entry count != 0
    results.QueueAdd(packet.EntryA);
    if (packet.TwoEntries == true) results.QueueAdd(packet.EntryB);
}
nextSendIndex = packet.LowestMissingIndex; // unconditional
```

Note `nextSendIndex` is taken **directly and unconditionally** from the
machine — the app does not advance it itself.

### Streaming loop

`ResultsLoop` writes one frame to `DATA_LOAD_SEND` **every 100 ms**
(`Task.Delay(100)`), and keeps going with entry count `0` after the notes run
out (`"...with no further note entries"`) — which is what keeps results
flowing during the tail of a track. Writes are write-without-response with
retry.

> Quirk: `NotePacket`'s list constructor assigns **both** `EntryA` and `EntryB`
> from `notes[index]` — the same note twice — while setting `TwoEntries` when
> two or more remain. This looks like a genuine bug in the shipped app. Sending
> `notes[index]` and `notes[index+1]` instead works correctly on hardware and
> is what `liteboxer.py` does.

---

## 6. Hit data — verified working

Hits arrive on `DATA_LOAD_RECEIVE` as result notes with `DidPunch` set,
carrying `Pad`, `Force` (0–127) and the **actual** punch time in `TimeMS`.
**[C, hardware-verified]**

A six-target track returned, in order:

```
HIT  pad=Top           force=4   t=998ms      (scheduled 1000)
HIT  pad=TopLeft       force=3   t=3047ms     (scheduled 3000)
HIT  pad=TopRight      force=3   t=5011ms     (scheduled 5000)
HIT  pad=BottomLeft    force=3   t=7006ms     (scheduled 7000)
HIT  pad=BottomRight   force=4   t=8957ms     (scheduled 9000)
HIT  pad=Bottom        force=5   t=11008ms    (scheduled 11000)
```

Richer telemetry exists in the codebase but is not on this path — a log format
string reads `WithdrawMS | Acceleration | Speed | Distance | Unix Sec | Unix MS`,
and LB Go has `GOPunch: Chirality | Packet Number | Punch Number | Punch Type |
AttackMS`. Where the extended floor-stand telemetry surfaces is still **[I]**.

---

## 6b. TIME_SYNC — the audio clock

`SyncTimes.GenerateSyncMessage()` produces **12 bytes** written to
`TIME_SYNC_SEND` **[C]**:

| Bytes | Field |
|---|---|
| 0–3 | `lastDeviceTimestamp` — the raw 4 bytes the machine last sent on `TIME_SYNC_RECEIVE` |
| 4–7 | local time when that was received (uint32 LE) |
| 8–11 | **current audio playback position** (uint32 LE), from a `getPlaybackTime()` callback |

This is a request/response clock sync: the machine notifies with a 4-byte
timestamp, the app replies with the triple. It is how the official app keeps
light choreography locked to streamed music.

**Not needed for playback.** In testing the machine never sent a
`TIME_SYNC_RECEIVE` notification, and pushing *unsolicited* 12-byte sync
messages made it stop replying on `DATA_LOAD_RECEIVE` altogether. Note
`TimeMS` in each note is absolute from track start, so the machine schedules
targets on its own clock without any sync. `liteboxer.py` leaves this off
(`--time-sync` to experiment).

---

## 7. Connection sequence

`InitialSetup()` does, in order **[C]**:

1. `SetLaneTime(...)`
2. `SetForceThresholds(...)` — from `HeadUnitValues.PuckThresholds`
3. Firmware version query (DFU control point — **omit in our client**)

Then `EnterPlaymode()` to leave idle, and `KeepAlive()` running every 15 s.

Sequence used by `liteboxer.py`, **verified working on hardware**:

1. Connect to the device advertising as `Litebox`
2. Subscribe to `DATA_LOAD_RECEIVE` **and** `TIME_SYNC_RECEIVE` notifications *first*
3. Write lane time to `ANIMATION_SPEED`
4. `EnterPlaymode`: `LB_CONTROL_PT <- [0x04, 0x01, 0x02]`, then `PLAYBACK_STATUS <- [0x01]`
5. Start the 15 s keepalive
6. Stream 19-byte NotePackets to `DATA_LOAD_SEND` in response to flow control
7. On exit: `PLAYBACK_STATUS <- [0x00]`, `LB_CONTROL_PT <- [0x08, 0x00]`

Concretely:

```
subscribe ADB53C85                       (DATA_LOAD_RECEIVE notifications)
09B4041F <- e8 03                        lane time 1000 ms
A5887CE9 <- 04 01 02                     EnterPlaymode(allGreen: true)
29EDA14A <- 01                           play
A5887CE9 <- 05 00                        begin track load
8BE06E5E <- <19-byte NotePacket>         every 100 ms, forever
09B4041F <- e8 03                        every 15 s (keepalive)
...
29EDA14A <- 00                           stop
A5887CE9 <- 08 00                        idle
```

Note that `EnterPlaymode(allGreen: true)` on its own lights **all six targets
green** and makes them hit-responsive — useful as a smoke test with no track at
all.

---

## 8. Still open

- Exact 20-byte menu-mode colour packing (`StartMenu`)
- `IDLE_SELECT` value meanings (attract-pattern choice)
- `SetForceThresholds` payload layout (`HeadUnitValues.PuckThresholds` is an `int[6]`)
- Meaning of control-point ack bytes 1–2
- The two characteristics the app never names: `73D02279`, `7AFE26C5`
- Where the extended punch telemetry (`Acceleration`/`Speed`/`Distance`) surfaces
- Whether the machine ever initiates `TIME_SYNC_RECEIVE`, and under what conditions

## 9. Reproducing the extraction

```sh
unzip -q Liteboxer*.xapk -d xapk
unzip -q xapk/com.liteboxer.Liteboxer.apk -d base
pip3 install lz4 dnfile
python3 tools/unblob.py                      # -> assemblies/*.dll  (200 files)
python3 tools/ildasm.py assemblies/Liteboxer.Services.HeadUnit.dll > headunit_il.txt
```

`tools/unblob.py` parses the `XABA` v1 AssemblyStore: a 20-byte header, then
`local_entry_count` 24-byte descriptors, then two hash-index arrays. Each payload is an
`XALZ` LZ4 container. Note that the index embedded in the `XALZ` header is a *global
mapping* index and does **not** match the manifest's `Blob idx`; the descriptor's position
in the array does.

`tools/ildasm.py` is a small CIL disassembler (dnfile + a hand-built opcode table) that
resolves string literals and member tokens — enough to read byte-level constants out of
async state machines.
