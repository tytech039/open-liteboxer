#!/usr/bin/env python3
"""Minimal CIL disassembler for .NET assemblies, built on dnfile.

Usage:
    ildasm.py <assembly.dll> [TypeNameSubstring] [MethodNameSubstring]

Prints each matching method's IL with string literals, field and method
tokens resolved to names -- enough to recover byte-level protocol constants.
"""
import sys
import struct
import dnfile

# opcode -> (name, operand_kind)
#   kinds: none, i1, u1, i2, i4, i8, r4, r8, tok, str, br1, br4, sw, var1, var2
OP1 = {
    0x00: ("nop", "none"), 0x01: ("break", "none"),
    0x02: ("ldarg.0", "none"), 0x03: ("ldarg.1", "none"),
    0x04: ("ldarg.2", "none"), 0x05: ("ldarg.3", "none"),
    0x06: ("ldloc.0", "none"), 0x07: ("ldloc.1", "none"),
    0x08: ("ldloc.2", "none"), 0x09: ("ldloc.3", "none"),
    0x0A: ("stloc.0", "none"), 0x0B: ("stloc.1", "none"),
    0x0C: ("stloc.2", "none"), 0x0D: ("stloc.3", "none"),
    0x0E: ("ldarg.s", "var1"), 0x0F: ("ldarga.s", "var1"),
    0x10: ("starg.s", "var1"), 0x11: ("ldloc.s", "var1"),
    0x12: ("ldloca.s", "var1"), 0x13: ("stloc.s", "var1"),
    0x14: ("ldnull", "none"), 0x15: ("ldc.i4.m1", "none"),
    0x16: ("ldc.i4.0", "none"), 0x17: ("ldc.i4.1", "none"),
    0x18: ("ldc.i4.2", "none"), 0x19: ("ldc.i4.3", "none"),
    0x1A: ("ldc.i4.4", "none"), 0x1B: ("ldc.i4.5", "none"),
    0x1C: ("ldc.i4.6", "none"), 0x1D: ("ldc.i4.7", "none"),
    0x1E: ("ldc.i4.8", "none"), 0x1F: ("ldc.i4.s", "i1"),
    0x20: ("ldc.i4", "i4"), 0x21: ("ldc.i8", "i8"),
    0x22: ("ldc.r4", "r4"), 0x23: ("ldc.r8", "r8"),
    0x25: ("dup", "none"), 0x26: ("pop", "none"),
    0x27: ("jmp", "tok"), 0x28: ("call", "tok"),
    0x29: ("calli", "tok"), 0x2A: ("ret", "none"),
    0x2B: ("br.s", "br1"), 0x2C: ("brfalse.s", "br1"),
    0x2D: ("brtrue.s", "br1"), 0x2E: ("beq.s", "br1"),
    0x2F: ("bge.s", "br1"), 0x30: ("bgt.s", "br1"),
    0x31: ("ble.s", "br1"), 0x32: ("blt.s", "br1"),
    0x33: ("bne.un.s", "br1"), 0x34: ("bge.un.s", "br1"),
    0x35: ("bgt.un.s", "br1"), 0x36: ("ble.un.s", "br1"),
    0x37: ("blt.un.s", "br1"), 0x38: ("br", "br4"),
    0x39: ("brfalse", "br4"), 0x3A: ("brtrue", "br4"),
    0x3B: ("beq", "br4"), 0x3C: ("bge", "br4"),
    0x3D: ("bgt", "br4"), 0x3E: ("ble", "br4"),
    0x3F: ("blt", "br4"), 0x40: ("bne.un", "br4"),
    0x41: ("bge.un", "br4"), 0x42: ("bgt.un", "br4"),
    0x43: ("ble.un", "br4"), 0x44: ("blt.un", "br4"),
    0x45: ("switch", "sw"),
    0x46: ("ldind.i1", "none"), 0x47: ("ldind.u1", "none"),
    0x48: ("ldind.i2", "none"), 0x49: ("ldind.u2", "none"),
    0x4A: ("ldind.i4", "none"), 0x4B: ("ldind.u4", "none"),
    0x4C: ("ldind.i8", "none"), 0x4D: ("ldind.i", "none"),
    0x4E: ("ldind.r4", "none"), 0x4F: ("ldind.r8", "none"),
    0x50: ("ldind.ref", "none"), 0x51: ("stind.ref", "none"),
    0x52: ("stind.i1", "none"), 0x53: ("stind.i2", "none"),
    0x54: ("stind.i4", "none"), 0x55: ("stind.i8", "none"),
    0x56: ("stind.r4", "none"), 0x57: ("stind.r8", "none"),
    0x58: ("add", "none"), 0x59: ("sub", "none"),
    0x5A: ("mul", "none"), 0x5B: ("div", "none"),
    0x5C: ("div.un", "none"), 0x5D: ("rem", "none"),
    0x5E: ("rem.un", "none"), 0x5F: ("and", "none"),
    0x60: ("or", "none"), 0x61: ("xor", "none"),
    0x62: ("shl", "none"), 0x63: ("shr", "none"),
    0x64: ("shr.un", "none"), 0x65: ("neg", "none"),
    0x66: ("not", "none"),
    0x67: ("conv.i1", "none"), 0x68: ("conv.i2", "none"),
    0x69: ("conv.i4", "none"), 0x6A: ("conv.i8", "none"),
    0x6B: ("conv.r4", "none"), 0x6C: ("conv.r8", "none"),
    0x6D: ("conv.u4", "none"), 0x6E: ("conv.u8", "none"),
    0x6F: ("callvirt", "tok"), 0x70: ("cpobj", "tok"),
    0x71: ("ldobj", "tok"), 0x72: ("ldstr", "str"),
    0x73: ("newobj", "tok"), 0x74: ("castclass", "tok"),
    0x75: ("isinst", "tok"), 0x76: ("conv.r.un", "none"),
    0x79: ("unbox", "tok"), 0x7A: ("throw", "none"),
    0x7B: ("ldfld", "tok"), 0x7C: ("ldflda", "tok"),
    0x7D: ("stfld", "tok"), 0x7E: ("ldsfld", "tok"),
    0x7F: ("ldsflda", "tok"), 0x80: ("stsfld", "tok"),
    0x81: ("stobj", "tok"),
    0x82: ("conv.ovf.i1.un", "none"), 0x83: ("conv.ovf.i2.un", "none"),
    0x84: ("conv.ovf.i4.un", "none"), 0x85: ("conv.ovf.i8.un", "none"),
    0x86: ("conv.ovf.u1.un", "none"), 0x87: ("conv.ovf.u2.un", "none"),
    0x88: ("conv.ovf.u4.un", "none"), 0x89: ("conv.ovf.u8.un", "none"),
    0x8A: ("conv.ovf.i.un", "none"), 0x8B: ("conv.ovf.u.un", "none"),
    0x8C: ("box", "tok"), 0x8D: ("newarr", "tok"),
    0x8E: ("ldlen", "none"), 0x8F: ("ldelema", "tok"),
    0x90: ("ldelem.i1", "none"), 0x91: ("ldelem.u1", "none"),
    0x92: ("ldelem.i2", "none"), 0x93: ("ldelem.u2", "none"),
    0x94: ("ldelem.i4", "none"), 0x95: ("ldelem.u4", "none"),
    0x96: ("ldelem.i8", "none"), 0x97: ("ldelem.i", "none"),
    0x98: ("ldelem.r4", "none"), 0x99: ("ldelem.r8", "none"),
    0x9A: ("ldelem.ref", "none"), 0x9B: ("stelem.i", "none"),
    0x9C: ("stelem.i1", "none"), 0x9D: ("stelem.i2", "none"),
    0x9E: ("stelem.i4", "none"), 0x9F: ("stelem.i8", "none"),
    0xA0: ("stelem.r4", "none"), 0xA1: ("stelem.r8", "none"),
    0xA2: ("stelem.ref", "none"), 0xA3: ("ldelem", "tok"),
    0xA4: ("stelem", "tok"), 0xA5: ("unbox.any", "tok"),
    0xB3: ("conv.ovf.i1", "none"), 0xB4: ("conv.ovf.u1", "none"),
    0xB5: ("conv.ovf.i2", "none"), 0xB6: ("conv.ovf.u2", "none"),
    0xB7: ("conv.ovf.i4", "none"), 0xB8: ("conv.ovf.u4", "none"),
    0xB9: ("conv.ovf.i8", "none"), 0xBA: ("conv.ovf.u8", "none"),
    0xC2: ("refanyval", "tok"), 0xC3: ("ckfinite", "none"),
    0xC6: ("mkrefany", "tok"), 0xD0: ("ldtoken", "tok"),
    0xD1: ("conv.u2", "none"), 0xD2: ("conv.u1", "none"),
    0xD3: ("conv.i", "none"), 0xD4: ("conv.ovf.i", "none"),
    0xD5: ("conv.ovf.u", "none"), 0xD6: ("add.ovf", "none"),
    0xD7: ("add.ovf.un", "none"), 0xD8: ("mul.ovf", "none"),
    0xD9: ("mul.ovf.un", "none"), 0xDA: ("sub.ovf", "none"),
    0xDB: ("sub.ovf.un", "none"), 0xDC: ("endfinally", "none"),
    0xDD: ("leave", "br4"), 0xDE: ("leave.s", "br1"),
    0xDF: ("stind.i", "none"), 0xE0: ("conv.u", "none"),
}
OP2 = {  # prefixed with 0xFE
    0x00: ("arglist", "none"), 0x01: ("ceq", "none"),
    0x02: ("cgt", "none"), 0x03: ("cgt.un", "none"),
    0x04: ("clt", "none"), 0x05: ("clt.un", "none"),
    0x06: ("ldftn", "tok"), 0x07: ("ldvirtftn", "tok"),
    0x09: ("ldarg", "var2"), 0x0A: ("ldarga", "var2"),
    0x0B: ("starg", "var2"), 0x0C: ("ldloc", "var2"),
    0x0D: ("ldloca", "var2"), 0x0E: ("stloc", "var2"),
    0x0F: ("localloc", "none"), 0x11: ("endfilter", "none"),
    0x12: ("unaligned.", "u1"), 0x13: ("volatile.", "none"),
    0x14: ("tail.", "none"), 0x15: ("initobj", "tok"),
    0x16: ("constrained.", "tok"), 0x17: ("cpblk", "none"),
    0x18: ("initblk", "none"), 0x1A: ("rethrow", "none"),
    0x1C: ("sizeof", "tok"), 0x1D: ("refanytype", "none"),
    0x1E: ("readonly.", "none"),
}


class Asm:
    def __init__(self, path):
        self.pe = dnfile.dnPE(path)
        self.net = self.pe.net
        self.t = self.net.mdtables
        self.us = self.net.user_strings

    # ---- token resolution ----
    def row(self, table, rid):
        try:
            tbl = getattr(self.t, table, None)
            if tbl is None or rid < 1 or rid > len(tbl.rows):
                return None
            return tbl.rows[rid - 1]
        except Exception:
            return None

    def type_name(self, table, rid):
        r = self.row(table, rid)
        if r is None:
            return f"{table}[{rid}]"
        ns = getattr(r, "TypeNamespace", "") or ""
        return f"{ns}.{r.TypeName}" if ns else str(r.TypeName)

    def parent_of_member(self, r):
        """Best-effort owning-type name for a MemberRef row."""
        cls = getattr(r, "Class", None)
        if cls is None:
            return "?"
        ri = getattr(cls, "row_index", None)
        tbl = type(getattr(cls, "row", None)).__name__ if getattr(cls, "row", None) else ""
        if "TypeRef" in tbl:
            return self.type_name("TypeRef", ri)
        if "TypeDef" in tbl:
            return self.type_name("TypeDef", ri)
        if "TypeSpec" in tbl:
            return "TypeSpec"
        return tbl or "?"

    def resolve(self, tok):
        table, rid = tok >> 24, tok & 0xFFFFFF
        if table == 0x70:  # string
            return "<string>"
        if table == 0x0A:  # MemberRef
            r = self.row("MemberRef", rid)
            if r is None:
                return f"MemberRef[{rid}]"
            return f"{self.parent_of_member(r)}::{r.Name}"
        if table == 0x06:  # MethodDef
            r = self.row("MethodDef", rid)
            return f"{self.owner.get(rid,'?')}::{r.Name}" if r else f"MethodDef[{rid}]"
        if table == 0x04:  # Field
            r = self.row("Field", rid)
            return f"fld {r.Name}" if r else f"Field[{rid}]"
        if table == 0x01:
            return self.type_name("TypeRef", rid)
        if table == 0x02:
            return self.type_name("TypeDef", rid)
        if table == 0x1B:
            return f"TypeSpec[{rid}]"
        if table == 0x2B:
            return f"MethodSpec[{rid}]"
        return f"tok[{table:#x}:{rid}]"

    def ldstr(self, tok):
        rid = tok & 0xFFFFFF
        try:
            s = self.us.get(rid)
            return repr(s.value if hasattr(s, "value") else s)
        except Exception:
            return f"us[{rid}]"

    # ---- method bodies ----
    def build_index(self):
        """Map MethodDef rid -> declaring type name, and gather methods per type."""
        self.owner = {}
        self.types = []
        for r in self.t.TypeDef.rows:
            ns = getattr(r, "TypeNamespace", "") or ""
            name = f"{ns}.{r.TypeName}" if ns else str(r.TypeName)
            # dnfile resolves MethodList to a list of MDTableIndex objects
            rids = [m.row_index for m in (r.MethodList or [])]
            self.types.append((name, rids))
            for rid in rids:
                self.owner[rid] = name

    def body(self, rva):
        if not rva:
            return None, []
        off = self.pe.get_offset_from_rva(rva)
        d = self.pe.__data__
        b0 = d[off]
        if (b0 & 0x3) == 0x2:  # tiny
            size = b0 >> 2
            return d[off + 1 : off + 1 + size], []
        # fat
        flags, maxstack, csize, lvtok = struct.unpack_from("<HHII", d, off)
        hsize = (flags >> 12) * 4
        il = d[off + hsize : off + hsize + csize]
        return il, []

    def disasm(self, il):
        out = []
        i = 0
        while i < len(il):
            start = i
            op = il[i]
            i += 1
            if op == 0xFE:
                name, kind = OP2.get(il[i], (f"unk.fe.{il[i]:02x}", "none"))
                i += 1
            else:
                name, kind = OP1.get(op, (f"unk.{op:02x}", "none"))
            arg = ""
            if kind == "none":
                pass
            elif kind in ("i1",):
                arg = str(struct.unpack_from("<b", il, i)[0]); i += 1
            elif kind in ("u1", "var1"):
                arg = str(il[i]); i += 1
            elif kind == "var2":
                arg = str(struct.unpack_from("<H", il, i)[0]); i += 2
            elif kind == "i4":
                v = struct.unpack_from("<i", il, i)[0]; i += 4
                arg = f"{v} (0x{v & 0xFFFFFFFF:X})"
            elif kind == "i8":
                arg = str(struct.unpack_from("<q", il, i)[0]); i += 8
            elif kind == "r4":
                arg = str(struct.unpack_from("<f", il, i)[0]); i += 4
            elif kind == "r8":
                arg = str(struct.unpack_from("<d", il, i)[0]); i += 8
            elif kind == "tok":
                t = struct.unpack_from("<I", il, i)[0]; i += 4
                arg = self.resolve(t)
            elif kind == "str":
                t = struct.unpack_from("<I", il, i)[0]; i += 4
                arg = self.ldstr(t)
            elif kind == "br1":
                off = struct.unpack_from("<b", il, i)[0]; i += 1
                arg = f"IL_{i + off:04x}"
            elif kind == "br4":
                off = struct.unpack_from("<i", il, i)[0]; i += 4
                arg = f"IL_{i + off:04x}"
            elif kind == "sw":
                cnt = struct.unpack_from("<I", il, i)[0]; i += 4
                tgts = struct.unpack_from(f"<{cnt}i", il, i); i += 4 * cnt
                arg = ", ".join(f"IL_{i + t:04x}" for t in tgts)
            out.append(f"  IL_{start:04x}:  {name:<16} {arg}")
        return out


def main():
    path = sys.argv[1]
    tfilter = sys.argv[2].lower() if len(sys.argv) > 2 else ""
    mfilter = sys.argv[3].lower() if len(sys.argv) > 3 else ""

    a = Asm(path)
    a.build_index()
    md = a.t.MethodDef.rows

    for tname, rids in a.types:
        if tfilter and tfilter not in tname.lower():
            continue
        shown = []
        for rid in rids:
            if rid < 1 or rid > len(md):
                continue
            m = md[rid - 1]
            mn = str(m.Name)
            if mfilter and mfilter not in mn.lower():
                continue
            shown.append((rid, m, mn))
        if not shown:
            continue
        print(f"\n{'='*78}\nTYPE {tname}\n{'='*78}")
        for rid, m, mn in shown:
            print(f"\n--- {tname}::{mn}   (MethodDef rid {rid})")
            try:
                il, _ = a.body(m.Rva)
                if il is None:
                    print("   <no body>")
                    continue
                for line in a.disasm(il):
                    print(line)
            except Exception as e:
                print(f"   <error: {e}>")


if __name__ == "__main__":
    main()
