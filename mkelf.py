#!/usr/bin/env python3
"""Wrap a raw firmware image as an ARM ELF so Ghidra (via REA) can load it.

usage: mkelf.py IMAGE LOAD_ADDR OUT.elf [THUMB_FUNC_ADDR ...]

Each THUMB_FUNC_ADDR becomes a function symbol plus a "$t" mapping symbol,
which tells Ghidra to decode from there in Thumb mode.
"""
import struct
import sys

EM_ARM = 40
SHT_PROGBITS, SHT_SYMTAB, SHT_STRTAB = 1, 2, 3
SHF_ALLOC, SHF_EXECINSTR = 2, 4
STT_NOTYPE, STT_FUNC, STB_GLOBAL, STB_LOCAL = 0, 2, 1, 0


def build(image, base, funcs):
    strtab = bytearray(b"\0")
    syms = [struct.pack("<IIIBBH", 0, 0, 0, 0, 0, 0)]

    def name(s):
        off = len(strtab)
        strtab.extend(s.encode() + b"\0")
        return off

    for a in funcs:
        a &= ~1
        syms.append(struct.pack("<IIIBBH", name("$t"), a, 0, STB_LOCAL << 4 | STT_NOTYPE, 0, 1))
    first_global = len(syms)
    for a in funcs:
        a &= ~1
        syms.append(struct.pack("<IIIBBH", name(f"fn_{a:08x}"), a | 1, 0,
                                STB_GLOBAL << 4 | STT_FUNC, 0, 1))
    shstr = b"\0.text\0.symtab\0.strtab\0.shstrtab\0"
    ehsize, phsize, shsize = 52, 32, 40
    text_off = 0x1000
    sym_off = text_off + len(image)
    symdata = b"".join(syms)
    str_off = sym_off + len(symdata)
    shstr_off = str_off + len(strtab)
    sh_off = (shstr_off + len(shstr) + 3) & ~3

    eh = b"\x7fELF" + bytes([1, 1, 1, 0]) + bytes(8)
    eh += struct.pack("<HHIIIIIHHHHHH", 2, EM_ARM, 1, base, ehsize, sh_off,
                      0x05000000, ehsize, phsize, 1, shsize, 5, 4)
    ph = struct.pack("<IIIIIIII", 1, text_off, base, base, len(image), len(image), 5, 0x1000)
    sections = [
        bytes(shsize),
        struct.pack("<IIIIIIIIII", 1, SHT_PROGBITS, SHF_ALLOC | SHF_EXECINSTR, base,
                    text_off, len(image), 0, 0, 4, 0),
        struct.pack("<IIIIIIIIII", 7, SHT_SYMTAB, 0, 0, sym_off, len(symdata), 3,
                    first_global, 4, 16),
        struct.pack("<IIIIIIIIII", 15, SHT_STRTAB, 0, 0, str_off, len(strtab), 0, 0, 1, 0),
        struct.pack("<IIIIIIIIII", 23, SHT_STRTAB, 0, 0, shstr_off, len(shstr), 0, 0, 1, 0),
    ]
    out = bytearray(eh + ph)
    out.extend(bytes(text_off - len(out)))
    out.extend(image)
    out.extend(symdata)
    out.extend(strtab)
    out.extend(shstr)
    out.extend(bytes(sh_off - len(out)))
    for s in sections:
        out.extend(s)
    return bytes(out)


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    image = open(sys.argv[1], "rb").read()
    base = int(sys.argv[2], 0)
    funcs = [int(a, 0) for a in sys.argv[4:]]
    open(sys.argv[3], "wb").write(build(image, base, funcs))


if __name__ == "__main__":
    main()
