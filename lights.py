"""The phone's lights: the screen backlight, the keypad backlight and the
torch. The firmware only writes them; the live window shows them.

  0x8200153c (PMIC)        screen backlight, the white-LED driver (fpdoom's
                           ANA_WHTLED_CTRL): bit 0 = off, bits 1-4 = level.
                           Settings > Display settings > Backlight >
                           Intensity sets levels 1-7 (default 4); switching
                           on or off steps through the levels 10 ms apart.
  0x82001540 (PMIC)        keypad backlight: bit 11 = off, bits 12-15 =
                           level (always 9 here).
  0x8a000000 (GPIO bank 0) data bit 0 = torch LED, an output (direction
                           register +0x08). Up twice on the home screen
                           toggles it; holding up lights it while held.
"""
BACKLIGHT = 0x8200153c
BACKLIGHT_OFF = 1 << 0
MAX_LEVEL = 7               # the brightest Intensity setting
KEYPAD_LED = 0x82001540
KEYPAD_LED_OFF = 1 << 11
GPIO_DATA, GPIO_DIR = 0x8a000000, 0x8a000008
TORCH = 1 << 0
REGISTERS = (BACKLIGHT, KEYPAD_LED, GPIO_DATA, GPIO_DIR)


class Lights:
    def __init__(self, machine, log):
        self.m = machine
        self.log = log
        self.state = self.read()

    def backlight(self):
        """Screen backlight level, or None while it is off (also before the
        firmware first sets it)."""
        v = self.m.regs.get(BACKLIGHT)
        return None if v is None or v & BACKLIGHT_OFF else v >> 1 & 0xf

    def keypad(self):
        v = self.m.regs.get(KEYPAD_LED)
        return v is not None and not v & KEYPAD_LED_OFF

    def torch(self):
        return bool(self.m.regs.get(GPIO_DATA, 0) & self.m.regs.get(GPIO_DIR, 0) & TORCH)

    def read(self):
        return self.backlight(), self.keypad(), self.torch()

    @staticmethod
    def lit(state):
        backlight, keypad, torch = state
        return backlight is not None, keypad, torch

    def on_write(self):
        old, self.state = self.state, self.read()
        # level steps while on are not worth a line each
        for name, was, now in zip(("backlight", "keypad light", "torch"), self.lit(old), self.lit(self.state)):
            if was != now:
                self.log(f"{name} {'on' if now else 'off'}")
