"""Keypad controller (0x87000000) and the phone's keys.

Registers (fpdoom's keypad_base_t; this firmware's driver is flash 0x1efd8
(init) and 0x1ef54 (interrupt handler, INTC line 12)):
  +0x00 ctrl        the driver writes 0x000f1f05: enable, long key, row mask
                    0x0f in bits 16-23, column mask 0x1f in bits 8-15
  +0x04 int_en
  +0x08 int_raw     bits 0-3: key down in slot 0-3, bits 4-7: key up
  +0x0c int_mask    int_raw & int_en
  +0x10 int_clr     write 1s to clear
  +0x2c key_status  byte n describes slot n: bits 0-2 = keymap index // 5,
                    bits 4-6 = index % 5 (flash 0x1ee32); with bit 3 set the
                    driver drops key-down events (flash 0x1f140)
The keymap (40 16-bit key codes, index = column * 5 + row, 0xffff unused)
is read from dump/unpacked/keymap.bin when fphelper has extracted it from
the firmware; otherwise the TA-1423 layout below is used. Key codes follow
fpdoom's KEYPAD_CHAR_ENUM.

The red power key is not on the matrix: it is EIC input bit 1 on the PMIC
(Machine.eic_inputs) and works as the end/back key once the phone is up.
"""
from array import array

KPD_BASE = 0x87000000
INT_EN, INT_RAW, INT_MASK, INT_CLR, KEY_STATUS = 0x04, 0x08, 0x0c, 0x10, 0x2c
IRQ_LINE = 12
SLOTS = 4

KEY_NAMES = {
    0x01: "call", 0x04: "up", 0x05: "down", 0x06: "left", 0x07: "right",
    0x08: "lsoft", 0x09: "rsoft", 0x0d: "select", 0x23: "#", 0x2a: "*",
    **{0x30 + d: str(d) for d in range(10)},
}
POWER_KEY = "end"
NONE = 0xffff
# Nokia 105 TA-1423 (firmware MOCOR_20C_6531E_W23.19.4) keymap
DEFAULT_KEYMAP = (
    0x2a, 0x36, 0x39, 0x23, NONE, 0x32, 0x35, 0x38, 0x30, NONE,
    0x31, 0x34, 0x37, 0x33, NONE, 0x01, 0x08, 0x06, 0x04, NONE,
    0x0d, 0x05, 0x07, 0x09) + (NONE,) * 16


class Keypad:
    def __init__(self, machine, keymap_path, log):
        self.m = machine
        self.log = log
        codes = array("H", keymap_path.read_bytes()) if keymap_path.is_file() else DEFAULT_KEYMAP
        self.index = {KEY_NAMES[c]: i for i, c in enumerate(codes) if c in KEY_NAMES}
        self.raw = 0
        self.status = 0
        self.slots = [None] * SLOTS     # key held in each slot
        self.power_held = False
        self.presses = 0

    def names(self):
        return sorted(self.index) + [POWER_KEY]

    def reg(self, off):
        return self.m.regs.get(KPD_BASE + off, 0)

    def read_masked(self):
        return self.raw & self.reg(INT_EN)

    def on_clr(self, value):
        self.raw &= ~value
        self.m.regs[KPD_BASE + INT_CLR] = 0
        self.update_line()

    def update_line(self):
        self.m.intc.set_line(IRQ_LINE, self.raw & self.reg(INT_EN))

    def press(self, name):
        if name == POWER_KEY:
            self.power_held = True
            self.m.pmic.update()
        elif name not in self.slots:
            if None not in self.slots:
                self.log(f"key {name}: all {SLOTS} slots busy, dropped")
                return
            slot = self.slots.index(None)
            self.slots[slot] = name
            i = self.index[name]
            shift = 8 * slot
            self.status = self.status & ~(0xff << shift) | ((i % 5) << 4 | i // 5) << shift
            self.raw |= 1 << slot
            self.update_line()
        self.presses += 1
        self.log(f"key {name} down")

    def release(self, name):
        if name == POWER_KEY:
            self.power_held = False
            self.m.pmic.update()
        elif name in self.slots:
            slot = self.slots.index(name)
            self.slots[slot] = None
            self.raw |= 1 << (4 + slot)
            self.update_line()
        self.log(f"key {name} up")
