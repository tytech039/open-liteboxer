#!/usr/bin/env python3
"""Extract .NET assemblies from a Xamarin assemblies.blob (AssemblyStore 'XABA' v1).

Layout (xamarin-android AssemblyStoreHeader):
    magic 'XABA' | version | local_entry_count | global_entry_count | store_id
  then local_entry_count * AssemblyStoreAssemblyDescriptor:
    data_offset | data_size | debug_offset | debug_size | config_offset | config_size

Each assembly payload is normally wrapped in an 'XALZ' LZ4 container:
    magic 'XALZ' | descriptor_index u32 | uncompressed_size u32 | lz4 block

Names come from assemblies.manifest ("Blob idx" == descriptor index).
"""
import os
import struct
import sys

import lz4.block

BLOB = sys.argv[1] if len(sys.argv) > 1 else "base/assemblies/assemblies.blob"
MANIFEST = sys.argv[2] if len(sys.argv) > 2 else "base/assemblies/assemblies.manifest"
OUTDIR = sys.argv[3] if len(sys.argv) > 3 else "assemblies"

os.makedirs(OUTDIR, exist_ok=True)

names = {}
with open(MANIFEST) as fh:
    for line in fh:
        parts = line.split()
        if len(parts) == 5 and parts[0].startswith("0x"):
            names[int(parts[3])] = parts[4]

data = open(BLOB, "rb").read()
magic, version, local_count, global_count, store_id = struct.unpack_from("<5I", data, 0)
assert magic == 0x41424158, f"bad magic {magic:#x}"
print(f"XABA v{version}  entries={local_count} global={global_count} store={store_id}")

ok = plain = fail = 0
for i in range(local_count):
    off = 20 + i * 24
    d_off, d_size = struct.unpack_from("<II", data, off)
    payload = data[d_off : d_off + d_size]
    # Descriptor array position == manifest "Blob idx". (The index embedded in
    # the XALZ header is a global mapping index and does NOT match.)
    name = names.get(i, f"unknown_{i}")

    if payload[:4] == b"XALZ":
        _, usize = struct.unpack_from("<II", payload, 4)
        try:
            raw = lz4.block.decompress(payload[12:], uncompressed_size=usize)
        except Exception as e:
            print(f"  !! {name}: lz4 failed: {e}")
            fail += 1
            continue
    else:
        raw = payload  # stored uncompressed
        name = names.get(i, f"unknown_{i}")
        plain += 1

    if raw[:2] != b"MZ":
        print(f"  ?? {name}: not a PE (starts {raw[:4]!r})")
    with open(os.path.join(OUTDIR, name + ".dll"), "wb") as fh:
        fh.write(raw)
    ok += 1

print(f"\nextracted {ok} ({plain} uncompressed), failed {fail} -> {OUTDIR}/")
