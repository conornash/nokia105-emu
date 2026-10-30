"""Digital EIC on the main chip (0x8a001000): 16 debounced input lines.

It is the first of the five input controllers in the firmware's EIC table
(flash 0x2d108: 0x8a001000 x16, 0x82001200 x16 = the PMIC EIC in pmic.py,
then 0x8a001080/a0/c0 x8). The kernel driver (kern 0x403e1f6..0x403e3d2)
uses the same layout as the PMIC EIC:
  +0x00 data (debounced level)   +0x04 data mask
  +0x14 polarity                 +0x18 interrupt enable
  +0x1c raw status               +0x20 masked status
  +0x24 write-1 clear            +0x28 re-arm after a channel is handled
Interrupts reach the ARM on INTC line 11 (handler kern 0x403e444). As with
the PMIC EIC, a line is pending while it is enabled and its level equals
the polarity; the driver flips the polarity to wait for the next change.

Inputs, from the kernel's input table (RAM 0x430a060, 16 bytes per id:
id, type 2 = EIC, active level at +8, line at +0xa):
  line 1  headset detect (input id 0x11), active low: low = plugged in
          (kern 0x4000996 sets the state the status bar shows, 0x430a620)
Other lines read 0. The TA-1557 never reads this EIC: its headset detect is
PMIC EIC line 4 (Machine.eic_inputs), which follows the same plug state.
"""
EIC_BASE = 0x8a001000
DATA, DMSK, IEV, IE, RIS, MIS, IC = 0x00, 0x04, 0x14, 0x18, 0x1c, 0x20, 0x24
IRQ_LINE = 11
HEADSET = 1 << 1


class DigitalEic:
    def __init__(self, machine, headset):
        self.m = machine
        self.headset = headset

    def reg(self, off):
        return self.m.regs.get(EIC_BASE + off, 0)

    def level(self):
        return 0 if self.headset else HEADSET

    def raw(self):
        return ~(self.level() ^ self.reg(IEV)) & 0xffff

    def masked(self):
        return self.raw() & self.reg(IE)

    def on_clr(self, value):
        self.m.regs[EIC_BASE + IC] = 0
        self.update()

    def update(self):
        self.m.intc.set_line(IRQ_LINE, self.masked())

    def set_headset(self, plugged):
        self.headset = plugged
        self.update()
        self.m.pmic.update()    # the TA-1557's headset is PMIC EIC line 4
