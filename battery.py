"""The battery and the charger.

The battery is measured on ADC channel 2 (register value 0x62): with the
calibration below, mV = 690 + 1.25 * raw. The charge manager (thread
CHGMNG_SRV, message switch at flash 0x1baa6) reads it, turns it into a
level through a 12-point table (flash 0x1b494) and warns and powers off
when it runs low.

Plugging the charger in raises PMIC EIC input 0 (kernel input 7, callback
kern 0x40008e8). Its handler (flash 0x1bdfe) ignores the level and reads
the charger status at 0x8200155c: bit 2 = charger present (flash 0x212ac),
else it reports the charger gone. While it charges, a check every 500 ms
(flash 0x1b760) measures the charger voltage on ADC channel 3 (0x63, the
small scale, times 1000/74 by flash 0x1b58a) and stops if it reads under
4300 mV four times in a row. Status bit 4 (flash 0x212a2) set for ten
checks in a row ends the charge as done (reason 4: "Battery full. Unplug
charger from socket."); bit 1 (flash 0x212b6) or bit 0 (0x212c0) stops it
as a fault (reason 5: "Not charging"). Bit 0 of 0x82001550 switches
charging off (flash 0x210da). The emulated battery charges while the
charger is in and charging is on, at --charge-minutes from empty to full,
sets bit 4 when full, and does not drain by itself.

eFuse (0x82002000): one 32-bit word per block, selected by writing the block
number to both halves of +0x08 (flash 0x21692) and read from +0x00. The ADC
calibration is built from it (kern 0x4015728): the battery scale from block
5 bits 28-21 and 20-13 (flash 0x1df08: 0 gives 1.25 mV per step, 690 mV at
raw 0), and the small scale, used for the charger voltage, from block 5
bits 12-5 and block 6 bits 29-22 (flash 0x1df66), which must be at least 46
or the small scale stays uncalibrated and every reading converts to 0 mV.
"""

EFUSE_DATA, EFUSE_BLOCK = 0x82002000, 0x82002008
# small scale: raw = (field + 690) * 4 at 1000 mV, (field - 46) * 4 at 100 mV
SMALL_RAW_1000, SMALL_RAW_100 = 3276, 328
EFUSE = {
    5: (SMALL_RAW_100 // 4 + 46) << 5,
    6: (SMALL_RAW_1000 // 4 - 690) << 22,
}

EIC_CHARGER = 1 << 0            # PMIC EIC input 0
CHG_STATUS = 0x8200155c
CHG_PRESENT, CHG_DONE = 1 << 2, 1 << 4
CHG_CTRL = 0x82001550
CHG_OFF = 1 << 0
ADC_VBAT, ADC_VCHG = 2, 3       # channels
VCHG_DIVIDER = 74 / 1000        # charger voltage at the ADC
CHARGER_MV = 5000
EMPTY_MV, FULL_MV = 3400, 4200
MIN_MV, MAX_MV = 3000, FULL_MV


def efuse_read(m):
    return EFUSE.get(m.regs.get(EFUSE_BLOCK, 0) & 0xffff, 0)


def vbat_raw(mv):
    return max(0, min(0xfff, round((mv - 690) / 1.25)))


def small_raw(mv):
    """Raw small-scale reading for mV at the ADC pin."""
    raw = SMALL_RAW_100 + (SMALL_RAW_1000 - SMALL_RAW_100) * (mv - 100) / 900
    return max(0, min(0xfff, round(raw)))


class Battery:
    def __init__(self, machine, mv, charger, charge_minutes, log):
        self.m = machine
        self.log = log
        self.mv = float(mv)
        self.plugged = charger
        self.full = False
        self.charging = False   # as last logged
        self.since = None       # block charging was last accounted from
        self.mv_per_ms = (FULL_MV - EMPTY_MV) / (charge_minutes * 60_000)

    def charging_now(self):
        return self.plugged and not self.m.regs.get(CHG_CTRL, 0) & CHG_OFF

    def update(self):
        """Bring the voltage up to now. The battery is only looked at when
        the firmware reads it, so it needs no timer of its own (which would
        change the timing of every run)."""
        now = self.m.nblocks
        if self.since is not None and not self.full:
            self.mv = min(FULL_MV, self.mv + self.mv_per_ms * (now - self.since) / self.m.blocks_per_ms)
        self.since = now if self.charging_now() else None
        if self.plugged and self.mv >= FULL_MV and not self.full:
            self.full = True
            self.log("battery full")

    def adc(self, channel):
        """Raw reading of a battery ADC channel."""
        self.update()
        if channel == ADC_VBAT:
            return vbat_raw(self.mv)
        return small_raw(CHARGER_MV * VCHG_DIVIDER) if self.plugged else 0

    def status(self):
        self.update()
        if not self.plugged:
            return 0
        return CHG_PRESENT | (CHG_DONE if self.full else 0)

    def on_ctrl(self):
        self.update()
        now = self.charging_now()
        if now != self.charging:
            self.charging = now
            self.log(f"charging {'on' if now else 'off'}")

    def plug(self, plugged):
        if plugged == self.plugged:
            return
        self.update()
        self.plugged = plugged
        self.full = False
        self.log(f"charger {'plugged in' if plugged else 'unplugged'}")
        self.on_ctrl()
        self.m.pmic.update()

    def set_mv(self, mv):
        self.update()
        self.mv = float(max(MIN_MV, min(MAX_MV, mv)))
        self.full = False
        self.log(f"battery {self.mv / 1000:.2f} V")
        self.update()
