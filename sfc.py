"""SC6531E serial flash controller (SFC) and the SPI NOR chip behind it.

Register layout and command encoding follow the custom FDL's sfc.h/sfc.c
(tools/spreadtrum_flash/custom_fdl). A command is staged in up to twelve
cmd[] words; each has a type_info byte:
  bit 0     valid
  bits 1-2  bus width (1/2/4 lines; ignored here)
  bits 3-4  byte count - 1
  bits 5-6  op: 0 write, 1 read, 2 high-Z (dummy cycles)
  bit 7     send MSB first (addresses); otherwise LSB first (data)
Read bytes land MSB-aligned: the first byte received is bits 31..24.
Writing 1 to tbuf_clr empties cmd[] and type_info[] (the firmware's
driver only read-modify-writes type_info, so stale phases would otherwise
be sent). Setting soft_req bit 0 runs the command and sets status bit 0 (done,
cleared through int_clr). Status bit 1 reads as idle: the main firmware's
driver waits on it after every step, started or not, and commands here
complete instantly.
"""

SFC_BASE = 0x20a00000
SOFT_REQ = 0x04
TBUF_CLR = 0x08
INT_CLR = 0x0c
STATUS = 0x10
STATUS_IDLE = 2
CMD = 0x40
TYPE_INFO = 0x70

OP_WRITE, OP_READ, OP_HIGHZ = 0, 1, 2

READ_CMDS = {0x03: 0, 0x0b: 1, 0x3b: 1, 0x6b: 1, 0xbb: 1, 0xeb: 3}  # cmd -> dummy bytes
ERASE_SIZES = {0x20: 0x1000, 0x52: 0x8000, 0xd8: 0x10000}
UNIQUE_ID = bytes.fromhex("d1a2b3c4d5e6f708")  # made up; the real chip's is unknown


class SpiFlash:
    """Winbond W25Q32-style NOR flash; buf is a byte memoryview of the mapped image."""

    def __init__(self, buf, size, jedec_id=0xef4016, log=None, on_change=None):
        self.buf = buf
        self.size = size
        self.jedec_id = jedec_id
        self.status = [0, 0, 0]
        self.wel = False
        self.log = log or (lambda line: None)
        self.on_change = on_change or (lambda: None)   # after a program or erase

    def _addr(self, tx):
        return int.from_bytes(tx[1:4], "big") % self.size if len(tx) >= 4 else 0

    def transaction(self, tx, nread):
        cmd = tx[0]
        out = b""
        if cmd == 0x9f:
            out = self.jedec_id.to_bytes(3, "big")
        elif cmd in (0x05, 0x35, 0x15):
            i = {0x05: 0, 0x35: 1, 0x15: 2}[cmd]
            out = bytes([self.status[i] | (2 if i == 0 and self.wel else 0)]) * max(nread, 1)
        elif cmd == 0x06:
            self.wel = True
        elif cmd == 0x04:
            self.wel = False
        elif cmd in (0x01, 0x31, 0x11):
            if self.wel:
                first = {0x01: 0, 0x31: 1, 0x11: 2}[cmd]
                for i, b in enumerate(tx[1:4]):
                    if first + i < 3:
                        self.status[first + i] = b & ~1
            self.wel = False
        elif cmd in READ_CMDS:
            addr = self._addr(tx)
            out = bytes(self.buf[(addr + i) % self.size] for i in range(nread))
        elif cmd in (0x02, 0x32):
            addr = self._addr(tx)
            data = tx[4:]
            if self.wel:
                page = addr & ~0xff
                for i, b in enumerate(data):
                    a = page | ((addr + i) & 0xff)
                    self.buf[a] &= b
                self.log(f"flash program {addr:06x} +{len(data)}")
                self.on_change()
            self.wel = False
        elif cmd in ERASE_SIZES:
            size = ERASE_SIZES[cmd]
            addr = self._addr(tx) & ~(size - 1)
            if self.wel:
                self.buf[addr:addr + size] = b"\xff" * size
                self.log(f"flash erase {addr:06x} +{size:#x}")
                self.on_change()
            self.wel = False
        elif cmd in (0xc7, 0x60):
            if self.wel:
                self.buf[0:self.size] = b"\xff" * self.size
                self.log("flash chip erase")
                self.on_change()
            self.wel = False
        elif cmd == 0x4b:
            out = UNIQUE_ID
        elif cmd in (0x66, 0x99, 0xab, 0xb9, 0x75, 0x7a, 0x38, 0xff, 0xc0):
            pass  # reset, power-down/up, suspend/resume, QPI enter/exit, read params
        else:
            self.log(f"flash: unhandled command {cmd:02x} tx={tx.hex()} nread={nread}")
        return out.ljust(nread, b"\0")[:nread]


class Sfc:
    def __init__(self, machine, flash):
        self.m = machine
        self.flash = flash
        self.m.regs[SFC_BASE + STATUS] = STATUS_IDLE

    def reg(self, off):
        return self.m.regs.get(SFC_BASE + off, 0)

    def on_soft_req(self, value):
        if value & 1:
            self.execute()
            self.m.regs[SFC_BASE + SOFT_REQ] = value & ~1
            self.m.regs[SFC_BASE + STATUS] = self.reg(STATUS) | 1

    def on_tbuf_clr(self, value):
        if value & 1:
            for off in list(range(CMD, CMD + 48, 4)) + list(range(TYPE_INFO, TYPE_INFO + 12, 4)):
                self.m.regs[SFC_BASE + off] = 0

    def on_int_clr(self, value):
        self.m.regs[SFC_BASE + STATUS] = self.reg(STATUS) & ~1

    def execute(self):
        phases = []
        for i in range(12):
            t = (self.reg(TYPE_INFO + (i // 4) * 4) >> ((i % 4) * 8)) & 0xff
            if t & 1:
                phases.append((i, (t >> 5) & 3, ((t >> 3) & 3) + 1, t >> 7))
        tx = bytearray()
        reads = []
        for i, op, num, msb_first in phases:
            val = self.reg(CMD + 4 * i)
            if op == OP_READ:
                reads.append((i, num))
            elif op == OP_HIGHZ:
                tx += bytes(num)
            else:
                order = range(num - 1, -1, -1) if msb_first else range(num)
                tx += bytes((val >> (8 * k)) & 0xff for k in order)
        if not tx:
            return
        data = self.flash.transaction(bytes(tx), sum(n for _, n in reads))
        self.flash.log(f"cmd {tx[:8].hex()}{'..' if len(tx) > 8 else ''} "
                       f"phases {[(i, op, n) for i, op, n, _ in phases]} -> {data.hex()}")
        pos = 0
        for i, num in reads:
            word = 0
            for k in range(num):
                word |= data[pos + k] << (24 - 8 * k)
            pos += num
            self.m.regs[SFC_BASE + CMD + 4 * i] = word
