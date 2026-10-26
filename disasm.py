#!/usr/bin/env python3
"""Disassemble a raw firmware image.

usage: disasm.py FILE LOAD_ADDR START_ADDR [COUNT] [arm|thumb]
"""
import sys

import capstone


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    path = sys.argv[1]
    base = int(sys.argv[2], 0)
    start = int(sys.argv[3], 0)
    count = int(sys.argv[4], 0) if len(sys.argv) > 4 else 40
    mode = sys.argv[5] if len(sys.argv) > 5 else "arm"

    data = open(path, "rb").read()
    md = capstone.Cs(capstone.CS_ARCH_ARM,
                     capstone.CS_MODE_THUMB if mode == "thumb" else capstone.CS_MODE_ARM)
    off = start - base
    for i, ins in enumerate(md.disasm(data[off:off + count * 4], start)):
        if i >= count:
            break
        print(f"{ins.address:08x}: {ins.bytes.hex():<8} {ins.mnemonic:<8} {ins.op_str}")


if __name__ == "__main__":
    main()
