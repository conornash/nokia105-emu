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
driver waits on it after every step, started or not. The controller
itself completes every command at once; the NOR chip behind it does not
(see Busy timing below).

Busy timing. A status-register write, page program or erase leaves the
chip busy for its datasheet time, counted in emulated blocks. While busy,
status reads return WIP=1 with WEL still set (the latch clears when the
operation ends), the chip ignores every other command except suspend, and
reads of the flash (SFC read commands and XIP fetches or loads through the
memory-mapped window, which emu.py gates through on_busy) return 0xFF.
Suspend (0x75) ends the busy time of a program or erase and lets reads
through with SR2.SUS set; resume (0x7a) continues it for the time left.
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

# Busy time per operation, in ms of emulated time. Source: Winbond W25Q32FV
# datasheet, AC Electrical Characteristics, typical values: tW (write status
# register) 10 ms (max 15), tPP (page program) 0.7 ms (max 3), tSE (4 KB
# sector erase) 45 ms (max 400), tBE2 (64 KB block erase) 150 ms (max 2000).
# The datasheet's tBE1 (32 KB block erase) is 120 ms typical; 150 ms is used
# for it here as specified for this model. Chip erase is not modelled as busy
# (the firmware never issues it on the handset).
BUSY_MS_WRSR = 10.0
BUSY_MS_PROGRAM = 0.7
BUSY_MS_ERASE = {0x20: 45.0, 0x52: 150.0, 0xd8: 150.0}
# Commands the chip still accepts while busy: the status reads and suspend.
BUSY_OK = (0x05, 0x35, 0x15, 0x75)
SR1_WIP, SR1_WEL, SR2_SUS = 0x01, 0x02, 0x80


class SpiFlash:
    """Winbond W25Q32-style NOR flash; buf is a byte memoryview of the mapped image."""

    def __init__(self, buf, size, jedec_id=0xef4016, log=None, on_change=None,
                 now=None, schedule=None, blocks_per_ms=4000, on_busy=None):
        self.buf = buf
        self.size = size
        self.jedec_id = jedec_id
        self.status = [0, 0, 0]
        self.wel = False
        self.log = log or (lambda line: None)
        self.on_change = on_change or (lambda: None)   # after a program or erase
        # the emulated clock: now() is the current block, schedule(delay,
        # callback) runs callback that many blocks later
        self.now = now or (lambda: 0)
        self.schedule = schedule or (lambda delay, callback: None)
        self.blocks_per_ms = blocks_per_ms
        # on_busy(True) when reads of the chip become invalid, on_busy(False)
        # when they are valid again; emu.py swaps the XIP mapping to 0xFF
        self.on_busy = on_busy or (lambda busy: None)
        self.busy = None          # "wrsr", "program" or "erase" while busy
        self.busy_until = 0       # block at which the operation completes
        self.busy_token = 0       # tells a stale completion event apart
        self.suspended = None     # (kind, blocks left) while suspended

    def _start_busy(self, kind, ms=None, blocks=None):
        if blocks is None:
            blocks = max(1, round(ms * self.blocks_per_ms))
        self.busy = kind
        self.busy_until = self.now() + blocks
        self.busy_token += 1
        token = self.busy_token
        self.log(f"flash busy {kind} {blocks} blocks until {self.busy_until}")
        self.on_busy(True)
        self.schedule(blocks, lambda: self._finish(token))

    def _finish(self, token):
        if self.busy is not None and token == self.busy_token:
            self._end_busy()

    def _end_busy(self):
        self.log(f"flash ready after {self.busy}")
        self.busy = None
        self.wel = False      # the latch clears when the operation completes
        self.on_busy(False)

    def _check_busy(self):
        """End the operation if its time is up but the event has not run yet."""
        if self.busy is not None and self.now() >= self.busy_until:
            self._end_busy()

    def _addr(self, tx):
        return int.from_bytes(tx[1:4], "big") % self.size if len(tx) >= 4 else 0

    def transaction(self, tx, nread):
        cmd = tx[0]
        out = b""
        self._check_busy()
        if self.busy is not None and cmd not in BUSY_OK:
            # the chip ignores the command; what the controller clocks in is
            # undefined, 0xFF (the line idling high) is the deterministic choice
            self.log(f"flash: command {cmd:02x} ignored while busy ({self.busy})")
            return b"\xff" * nread
        if cmd == 0x9f:
            out = self.jedec_id.to_bytes(3, "big")
        elif cmd in (0x05, 0x35, 0x15):
            i = {0x05: 0, 0x35: 1, 0x15: 2}[cmd]
            v = self.status[i]
            if i == 0:
                v |= (SR1_WEL if self.wel else 0) | (SR1_WIP if self.busy else 0)
            elif i == 1 and self.suspended:
                v |= SR2_SUS
            out = bytes([v]) * max(nread, 1)
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
                self._start_busy("wrsr", ms=BUSY_MS_WRSR)
            else:
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
                self._start_busy("program", ms=BUSY_MS_PROGRAM)
            else:
                self.wel = False
        elif cmd in ERASE_SIZES:
            size = ERASE_SIZES[cmd]
            addr = self._addr(tx) & ~(size - 1)
            if self.wel:
                self.buf[addr:addr + size] = b"\xff" * size
                self.log(f"flash erase {addr:06x} +{size:#x}")
                self.on_change()
                self._start_busy("erase", ms=BUSY_MS_ERASE[cmd])
            else:
                self.wel = False
        elif cmd in (0xc7, 0x60):
            if self.wel:
                self.buf[0:self.size] = b"\xff" * self.size
                self.log("flash chip erase")
                self.on_change()
            self.wel = False
        elif cmd == 0x4b:
            out = UNIQUE_ID
        elif cmd == 0x75:
            # suspend: a program or erase pauses and reads are valid again
            # (tSUS, at most 20 us, is not modelled); a status write cannot
            # be suspended
            if self.busy in ("program", "erase"):
                left = max(1, self.busy_until - self.now())
                self.suspended = (self.busy, left)
                self.busy = None
                self.busy_token += 1
                self.log(f"flash suspend {self.suspended[0]}, {left} blocks left")
                self.on_busy(False)
        elif cmd == 0x7a:
            if self.suspended:
                kind, left = self.suspended
                self.suspended = None
                self.log(f"flash resume {kind}")
                self._start_busy(kind, blocks=left)
        elif cmd in (0x66, 0x99, 0xab, 0xb9, 0x38, 0xff, 0xc0):
            pass  # reset, power-down/up, QPI enter/exit, read params
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
