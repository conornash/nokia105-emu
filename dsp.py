"""Stand-in for the DSP (the GSM baseband core), which isn't emulated.

The ARM talks to it through shared memory at 0x30000000 (kern 0x400975e
onwards): it writes request/command bits to the 16-bit word at 0x30000000
and reads the DSP's acknowledge bits from 0x30000002 (bit 0 granted,
bit 1 message consumed, bit 2 command taken). The DSP interrupts the ARM
through 0x8b000140 (status; bit 2 is what the boot wait at kern 0x400973c
polls) and IRQ 21; the firmware clears bits by writing them to 0x8b000160
(flash 0x1dc24).

This stub acknowledges everything, and answers a start command (bit 2 or
3 in the request word) by raising DSP interrupt bit 2 a little later, as
the DSP would once it is running.
"""
SHARED_CMD = 0x30000000
INT_STS = 0x8b000140
INT_CLR = 0x8b000160
IRQ_LINE = 21
CMD_START = 0x4 | 0x8
INT_BOOTED = 1 << 2
ACK_ALL = 0x7
REPLY_DELAY_BLOCKS = 4000   # about 1 ms of emulated time


class Dsp:
    def __init__(self, machine):
        self.m = machine
        self.interrupts = 0

    def read_shared_cmd(self):
        return self.m.regs.get(SHARED_CMD, 0) | ACK_ALL << 16

    def on_shared_cmd(self, value):
        if value & CMD_START:
            self.m.schedule(REPLY_DELAY_BLOCKS, self.raise_booted)

    def raise_booted(self):
        self.m.regs[INT_STS] = self.m.regs.get(INT_STS, 0) | INT_BOOTED
        self.interrupts += 1
        self.update_line()

    def on_int_clr(self, value):
        self.m.regs[INT_STS] = self.m.regs.get(INT_STS, 0) & ~value
        self.update_line()

    def update_line(self):
        self.m.intc.set_line(IRQ_LINE, self.m.regs.get(INT_STS, 0) & 0xc)
