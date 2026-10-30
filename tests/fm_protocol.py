#!/usr/bin/env python3
"""FM receiver register protocol, as a driver sees it.

usage: .venv/bin/python -I tests/fm_protocol.py      (part of make test)

Builds the machine from the dump but runs no guest code: every access goes
through Machine._mmio_read / _mmio_write, the callbacks Unicorn calls for a
guest load or store, so the handler tables, the register file, the INTC
and the event queue are the ones the firmware gets. The sequences are the
stock ones from lughos/src/drivers/fm.h (kern addresses there and in
fm.py), plus what the stock Radio app never does: seek down across the
band edge, polling with the interrupt disabled, the antenna unplugged, the
RF part powered down, the clock stopped mid-seek, and reset.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import emu  # noqa: E402
import fm  # noqa: E402

B = fm.FM_BASE
INTC1_RAW = 0x80001004
LINE = 1 << (fm.IRQ_LINE - 32)
failures = []


def check(what, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {what}: {got} ({got:#x})"
          + ("" if ok else f", expected {want} ({want:#x})"))
    if not ok:
        failures.append(what)


class Driver:
    """Register accesses the way the guest makes them."""

    def __init__(self, m):
        self.m = m

    def rd(self, addr):
        return self.m._mmio_read(self.m.uc, 0, 4, addr)

    def wr(self, addr, value):
        self.m._mmio_write(self.m.uc, 0, 4, value & 0xffffffff, addr)

    def orr(self, addr, bits):
        self.wr(addr, self.rd(addr) | bits)

    def bic(self, addr, bits):
        self.wr(addr, self.rd(addr) & ~bits)

    def rf_write(self, reg, value):         # kern 0x040172e0
        self.wr(fm.RF_PORT, (reg << 16 & 0x7fff0000) | value & 0xffff)

    def rf_read(self, reg):                 # kern 0x040172fa
        self.wr(fm.RF_PORT, reg << 16 | 0x80000000)
        return self.rd(fm.RF_PORT) & 0xffff

    def line(self):
        return 1 if self.rd(INTC1_RAW) & LINE else 0

    def wait_ms(self, ms):
        self.m.nblocks += ms * emu.BLOCKS_PER_MS
        self.m.run_events()

    def power_up(self):                     # kern 0x0401740c, 0x0401760a
        self.orr(fm.APB_EB0, fm.FM_EB)
        self.orr(fm.APB_RST0, fm.FM_SOFT_RST)
        self.bic(fm.APB_RST0, fm.FM_SOFT_RST)
        self.rf_write(0x44, self.rf_read(0x44) & ~0xf000 | 0x5000)
        for off, value in ((0x000, 0x02001011), (0x004, 0x000112ee), (0x00c, 0x08017ed0),
                           (0x014, 0x00000014), (0x018, 0), (0x01c, 0x000155cc),
                           (0x020, 0x6401a5e0), (0x040, 0x00000bb8), (0x05c, 0x000001a6)):
            self.wr(B + off, value)         # from the stock init table, RAM 0x0430bd48

    def start(self, sti, khz, up=None):     # kern 0x040176ea, 0x0401785e
        self.bic(B + fm.EN, fm.EN_GO)
        if up is not None:
            (self.orr if up else self.bic)(B + fm.CTRL, fm.CTRL_SEEK_UP)
        self.wr(B + fm.STI, sti)
        self.wr(B + fm.CHAN, 0x80 << 20 | khz)
        self.wr(B + fm.INT_EN, 1)
        self.orr(B + fm.EN, fm.EN_GO)

    def irq_handler(self):                  # kern 0x04017354
        if not self.rd(B + fm.INT_STS) & 1:
            return None
        result = not self.rd(B + fm.RSSI_STS) & fm.RSSI_FAIL
        self.wr(B + fm.INT_EN, self.rd(B + fm.INT_EN) & 0xe)
        self.orr(B + fm.INT_CLR, 1)
        return result


def signal(d):
    """What the stock driver reports (kern 0x04018044): 512 - INPWR."""
    return 0x200 - (d.rd(B + fm.INPWR) & 0x1ff)


def main():
    out = ROOT / "out" / "test" / "fm_protocol"
    out.mkdir(parents=True, exist_ok=True)
    args = emu.parse_args(["--headset", "--start", "flash", "--out", str(out)])
    m = emu.Machine(args)
    d = Driver(m)

    print("clock and reset")
    d.wr(B + fm.SEEK_LOOP, 0x55)
    check("write with the clock off is dropped", d.rd(B + fm.SEEK_LOOP), 0)
    d.orr(fm.APB_EB0, fm.FM_EB)
    check("APB_EB0 reads back bit 29", d.rd(fm.APB_EB0) & fm.FM_EB, fm.FM_EB)
    d.wr(B + fm.SEEK_LOOP, 0x55)
    check("write with the clock on sticks", d.rd(B + fm.SEEK_LOOP), 0x55)
    d.orr(fm.APB_RST0, fm.FM_SOFT_RST)
    check("reads 0 in reset", d.rd(B + fm.SEEK_LOOP), 0)
    d.bic(fm.APB_RST0, fm.FM_SOFT_RST)
    check("reset cleared the register", d.rd(B + fm.SEEK_LOOP), 0)

    print("RF port")
    d.rf_write(0x37, 0x5540)
    check("RF 0x37 reads back", d.rf_read(0x37), 0x5540)
    check("port word after a read is (reg << 16) | value", d.rd(fm.RF_PORT), 0x00375540)

    d.power_up()
    check("RF 0x44 bits 15:12 after power-up", d.rf_read(0x44) >> 12 & 0xf, 5)

    print("tune 98.0 MHz (stereo, -70 dBm), interrupt enabled")
    d.start(fm.STI_TUNE, 98000)
    check("INT_STS bit 0", d.rd(B + fm.INT_STS), 1)
    check("line 44 raised", d.line(), 1)
    check("CHAN_FREQ", d.rd(B + fm.CHAN_FREQ), 98000)
    check("RSSI_STS: stereo + pilot, no fail, -70 dBm", d.rd(B + fm.RSSI_STS),
          fm.RSSI_STEREO | fm.RSSI_PILOT | (-70 & 0xff))
    check("signal = 512 - INPWR", signal(d), 70)
    result = d.irq_handler()
    check("handler result (1 = found)", int(result), 1)
    check("line 44 lowered", d.line(), 0)
    check("INT_STS cleared by INT_CLR", d.rd(B + fm.INT_STS), 0)
    check("INT_CLR reads 0", d.rd(B + fm.INT_CLR), 0)

    print("tune 99.0 MHz (nothing there)")
    d.start(fm.STI_TUNE, 99000)
    check("handler result (0 = failed)", int(d.irq_handler()), 0)
    check("signal at the noise floor", signal(d), 110)

    print("seek up from 98.1 MHz: the stock wrapper's retries")
    stops = []
    start = 98100
    for attempt in range(4):
        d.start(fm.STI_SEEK, start, up=True)
        check(f"seek {attempt + 1} not done at once", d.rd(B + fm.INT_STS), 0)
        d.wait_ms(20 * fm.SEEK_DWELL_MS)
        found = d.irq_handler()
        stops.append((d.rd(B + fm.CHAN_FREQ), d.rd(B + fm.SEEK_CNT), found))
        if found:
            break
        start = d.rd(B + fm.CHAN_FREQ) + 100
    check("seeks needed", len(stops), 3)
    check("first seek gives up at 100.0 MHz", stops[0][0], 100000)
    check("after SEEK_LOOP channels", stops[0][1], 20)
    check("third seek finds 102.7 MHz", stops[-1][0], 102700)
    check("in 7 channels", stops[-1][1], 7)

    print("seek down from 88.0 MHz wraps to the top of the band")
    d.start(fm.STI_SEEK, 88000, up=False)
    d.wait_ms(11 * fm.SEEK_DWELL_MS - 1)
    check("not done 1 ms early", d.rd(B + fm.INT_STS), 0)
    d.wait_ms(1)
    check("found", int(d.irq_handler()), 1)
    check("CHAN_FREQ", d.rd(B + fm.CHAN_FREQ), 107600)
    check("SEEK_CNT (88.0..87.5, then 108.0..107.6)", d.rd(B + fm.SEEK_CNT), 11)
    check("mono: no stereo bit", d.rd(B + fm.RSSI_STS) & fm.RSSI_STEREO, 0)

    print("polling with the interrupt disabled (the boot calibration's way)")
    d.bic(B + fm.EN, fm.EN_GO)
    d.wr(B + fm.STI, fm.STI_TUNE)
    d.wr(B + fm.CHAN, 0x80 << 20 | 95800)
    d.wr(B + fm.INT_EN, 0)
    d.orr(B + fm.EN, fm.EN_GO)
    check("INT_STS bit 0 set", d.rd(B + fm.INT_STS), 1)
    check("line 44 stays low", d.line(), 0)
    check("signal", signal(d), 55)
    d.wr(B + fm.INT_CLR, 1)

    print("antenna and RF power")
    m.eic.set_headset(False)
    check("unplugged: noise floor", signal(d), 110)
    m.eic.set_headset(True)
    check("plugged back in", signal(d), 55)
    d.rf_write(0x44, d.rf_read(0x44) & ~0xf000)
    check("RF 0x44 bits 15:12 = 0: noise floor", signal(d), 110)
    d.rf_write(0x44, d.rf_read(0x44) | 0x5000)

    print("clock stopped mid-seek")
    d.start(fm.STI_SEEK, 95900, up=True)
    d.bic(fm.APB_EB0, fm.FM_EB)
    d.wait_ms(200)
    d.orr(fm.APB_EB0, fm.FM_EB)
    check("seek abandoned: no completion", d.rd(B + fm.INT_STS), 0)
    check("line 44 low", d.line(), 0)

    m.fm.close()
    print(f"fm_protocol: {'FAIL, ' + str(len(failures)) + ' checks' if failures else 'ok'} "
          f"(log in {(out / 'fm.log').relative_to(ROOT)})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
