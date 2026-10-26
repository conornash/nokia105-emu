"""Interrupt controller (two banks) and the six general-purpose timers.

INTC layout, from the firmware's IRQ dispatcher (flash 0x11464/0x1666e),
which reads its status words through a pointer table at 0x42f90d8:
  bank 0 at 0x80000000, bank 1 at 0x80001000
  +0x00 IRQ status (raw & enable)    +0x20 FIQ status
  +0x04 IRQ raw                      +0x24 FIQ raw
  +0x08 IRQ enable (read, write 1 to set)
  +0x0c IRQ disable (write 1 to clear)
Interrupt number = 32 * bank + bit. Bank 1 bit 4 means "power-chip
interrupt": the dispatcher then reads 0x82001020 over the ADI.

Timers, from fpdoom's lcd_setup_timer (lcd_mono.h):
  timer i at 0x81000000 | (i / 3) << 12 | (i % 3) << 5
  timers 0, 1, 3, 4 count at 32768 Hz; 2 and 5 at 26 MHz
  +0x00 load  +0x04 value  +0x08 ctl (bit 7 run, bit 6 periodic)
  +0x0c int: bit 0 enable, bit 1 raw, bit 2 masked, write bit 3 to clear
  IRQ line: i + 4 for timers 0-2 (fpdoom); unknown for 3-5.
"""
INTC_BANKS = (0x80000000, 0x80001000)
IRQ_STS, IRQ_RAW, IRQ_EN, IRQ_DIS = 0x00, 0x04, 0x08, 0x0c

TIMER_BASES = (0x81000000, 0x81000020, 0x81000040, 0x81001000, 0x81001020, 0x81001040)
TIMER_HZ = (32768, 32768, 26_000_000, 32768, 32768, 26_000_000)
TIMER_LINE = (4, 5, 6, None, None, None)
LOAD, VALUE, CTL, INT = 0x00, 0x04, 0x08, 0x0c
CTL_RUN, CTL_PERIODIC = 0x80, 0x40
INT_EN, INT_RAW, INT_STS, INT_CLR = 1, 2, 4, 8


class Intc:
    def __init__(self, machine):
        self.m = machine
        self.raw = [0, 0]
        self.en = [0, 0]
        self.delivered = {}

    def pending(self):
        return bool(self.raw[0] & self.en[0] or self.raw[1] & self.en[1])

    def set_line(self, n, level):
        bank, bit = divmod(n, 32)
        if level:
            self.raw[bank] |= 1 << bit
        else:
            self.raw[bank] &= ~(1 << bit)

    def read(self, bank, off):
        if off == IRQ_STS:
            return self.raw[bank] & self.en[bank]
        if off == IRQ_RAW:
            return self.raw[bank]
        return self.en[bank]

    def write(self, bank, off, value):
        if off == IRQ_EN:
            self.en[bank] |= value
        elif off == IRQ_DIS:
            self.en[bank] &= ~value

    def note_delivery(self):
        for bank in (0, 1):
            pend = self.raw[bank] & self.en[bank]
            for bit in range(32):
                if pend >> bit & 1:
                    n = 32 * bank + bit
                    self.delivered[n] = self.delivered.get(n, 0) + 1


class Timer:
    def __init__(self, machine, index):
        self.m = machine
        self.i = index
        self.base = TIMER_BASES[index]
        self.hz = TIMER_HZ[index]
        self.start = None     # block count when started
        self.load = 0
        self.raw = False
        self.fires = 0

    def reg(self, off):
        return self.m.regs.get(self.base + off, 0)

    def ticks_since(self, block):
        return (self.m.nblocks - block) * self.hz // (1000 * self.m.blocks_per_ms)

    def blocks_for(self, ticks):
        return max(1, ticks * 1000 * self.m.blocks_per_ms // self.hz)

    def value(self):
        if self.start is None:
            return self.reg(VALUE)
        elapsed = self.ticks_since(self.start)
        if self.reg(CTL) & CTL_PERIODIC and self.load:
            return self.load - elapsed % (self.load + 1)
        return max(0, self.load - elapsed)

    def expiry_block(self):
        if self.start is None:
            return None
        return self.start + self.blocks_for(self.load + 1)

    def on_ctl(self, value):
        if value & CTL_RUN:
            if self.start is None:
                self.start = self.m.nblocks
                self.load = self.reg(LOAD)
        else:
            if self.start is not None:
                self.m.regs[self.base + VALUE] = self.value()
            self.start = None

    def on_int(self, value):
        if value & INT_CLR:
            self.raw = False
        self.m.regs[self.base + INT] = value & INT_EN
        self.update_line()

    def read_int(self):
        en = self.reg(INT) & INT_EN
        return en | (INT_RAW if self.raw else 0) | (INT_STS if self.raw and en else 0)

    def tick(self):
        """Called when nblocks reaches expiry_block()."""
        self.raw = True
        self.fires += 1
        if self.reg(CTL) & CTL_PERIODIC:
            self.start = self.expiry_block()
        else:
            self.start = None
        self.update_line()

    def update_line(self):
        line = TIMER_LINE[self.i]
        if line is not None:
            self.m.intc.set_line(line, self.raw and self.reg(INT) & INT_EN)
