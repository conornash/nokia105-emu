"""SC6531E hardware LZMA decompressor at 0x20e00000.

Register use inferred from the firmware's driver (flash 0x22630..0x22768)
and the kernel unpack it performs (0x22a38):
  +0x00  control: bit 0 start, bit 2 windowed mode, bit 3 unknown
         (0x22648); the driver read-modify-writes it through +0x04
  +0x04  control read-back
  +0x08  interrupts: bits 0-4 enable, bits 8-12 write 1 to clear,
         bits 16-20 masked status, bits 24-28 raw status
         (bit 24 done, bits 25-28 errors)
  +0x0c  source address (points at the 13-byte LZMA header)
  +0x10  destination address
  +0x14  source length (0 for the kernel: read until the stream ends)
  +0x18  destination length
  +0x1c  windowed mode: buffer the whole stream is decoded into
  +0x20  windowed mode: buffer length (0x1000, one demand-paged chunk)
  +0x24  windowed mode: offset of the slice copied to the destination
  +0x30  bytes produced
Demand paging uses the windowed mode: each 4 KB chunk is its own stream,
and the abort handler asks for the 1 KB slice holding the faulting address.
The stream format is Spreadtrum's LZMA variant (fphelper's lzmadec_sprd),
decoded through liblzma_sprd (built from lzma_sprd.c by `make`).
"""
import ctypes
import sys
from pathlib import Path

LZMA_BASE = 0x20e00000
START = 0x00
CTRL_RB = 0x04
INT = 0x08
SRC = 0x0c
DST = 0x10
SRC_LEN = 0x14
DST_LEN = 0x18
BUF = 0x1c
BUF_LEN = 0x20
WINDOW = 0x24
OUT_LEN = 0x30

CTRL_START = 1 << 0
CTRL_WINDOWED = 1 << 2

INT_DONE = 1 << 24
INT_ERROR = 1 << 25

_LIB = Path(__file__).resolve().parent / ("liblzma_sprd.dylib" if sys.platform == "darwin" else "liblzma_sprd.so")
if not _LIB.exists():
    raise SystemExit(f"{_LIB.name} not found: run `make` in {_LIB.parent} first")
_lib = ctypes.CDLL(str(_LIB))
_lib.lzma_decode.restype = ctypes.c_uint32


def decode(src, dst_len):
    ssz = ctypes.c_size_t(len(src))
    dsz = ctypes.c_size_t(dst_len)
    dst = ctypes.create_string_buffer(max(dst_len, 1))
    rc = _lib.lzma_decode(src, ctypes.byref(ssz), dst, ctypes.byref(dsz), 1)
    return rc, ssz.value, dst.raw[:dsz.value]


class LzmaEngine:
    def __init__(self, machine):
        self.m = machine
        self.raw = 0  # raw interrupt status, bits 24-28
        self.decodes = 0

    def reg(self, off):
        return self.m.regs.get(LZMA_BASE + off, 0)

    def read_ctrl(self):
        return self.reg(START) & ~CTRL_START

    def read_int(self):
        enable = self.reg(INT) & 0x1f
        return enable | (self.raw >> 8 & enable << 16) | self.raw

    def on_int_write(self, value):
        self.raw &= ~((value >> 8 & 0x1f) << 24)
        self.m.regs[LZMA_BASE + INT] = value & 0x1f

    def on_start(self, value):
        if not value & CTRL_START:
            return
        self.m.regs[LZMA_BASE + START] = value & ~CTRL_START
        src, dst = self.reg(SRC), self.reg(DST)
        src_len, dst_len = self.reg(SRC_LEN), self.reg(DST_LEN)
        data = self.m.read_span(src, src_len or None)
        if value & CTRL_WINDOWED:
            buf, buf_len, window = self.reg(BUF), self.reg(BUF_LEN), self.reg(WINDOW)
            rc, used, full = decode(data, buf_len)
            ok = rc == 0 or len(full) == buf_len
            self.m.uc.mem_write(buf, full)
            out = full[window:window + dst_len]
            note = f" via {buf:08x}[{window:#x}:]"
        else:
            rc, used, out = decode(data, dst_len)
            ok = rc == 0 or len(out) == dst_len
            note = ""
        self.m.uc.mem_write(dst, out)
        self.m.regs[LZMA_BASE + OUT_LEN] = len(out)
        self.raw |= INT_DONE if ok else INT_ERROR
        self.decodes += 1
        if self.decodes <= 3 or not ok:
            print(f"[block {self.m.nblocks}] lzma #{self.decodes}: {src:08x} -> {dst:08x}{note}, "
                  f"{len(out):#x} out{'' if ok else ' (error)'}")
