"""FM receiver: the digital block at 0x88002000 (INTC line 44), its RF
registers behind 0x8d000000, and a synthetic FM band for it to receive.

Register names are those of the 2013 Mocor SC6531 header fm_reg_v0.h (an
earlier FM block with the same layout up to +0xd0); what each register
does here is read from this handset's kernel driver (kern 0x040172b2..
0x040191bc). The register map, the driver's sequences and the RF access
protocol are also written up in lughos/src/drivers/fm.h.

Clock and reset (2013 sc6531_reg_global.h, and kern 0x0401740c):
  APB_EB0  (0x8b0000a8) bit 29 FM_EB: the block's clock. The Radio app's
           power-up (kern 0x04017b4e) sets 0x20000c00 here and 0x00200200
           in 0x8b0000ac; power-down clears bit 29 (kern 0x04017c0c).
  APB_RST0 (0x8b000068) bit 23 FM_SOFT_RST: pulsed by the power-up.
  While the clock is off or reset is held the block is stopped: a tune or
  seek in progress is abandoned, its registers read 0 and writes to them
  are dropped. Reset clears every register. (The Radio app reads the whole
  block once with the clock still off, kern 0x04017388, and discards it.)
  Power-up also sets 0x8b0002c8 bits 10 and 2 and 0x8b0002e4 bit 21; those
  are plain registers here.

Digital block (offsets from 0x88002000; all 32-bit accesses):
  +0x000 CTRL      bit 19 seek direction (1 = up); bit 22 set around a
                   tune and the status snapshot; bit 13 pulsed at power-up
  +0x004 EN        bit 31 starts a tune or seek on its 0 -> 1 edge
                   (kern 0x0401769c); clearing it stops the receiver
  +0x00c CHAN      bits 16:0 channel in kHz, bits 31:20 a per-mode
                   constant (100, 128, 412, 384) that goes with COEF
                   (+0x120); the model only uses the kHz
  +0x014 SEEK_LOOP channels a seek examines before giving up (stock 20)
  +0x018 STI       1 = seek, 2 = tune (0 = play CHAN, no interrupt)
  +0x01c           bits 16:0 bottom of the band, kHz (stock 87500)
  +0x020           bits 16:0 top of the band, kHz (stock 108000); bits
                   31:24 channel spacing, kHz (stock 100). In the v0 header
                   these are BAND_LMT and BAND_SPACE; the stock init table
                   (RAM 0x0430bd48) writes 0x000155cc and 0x6401a5e0.
  +0x030 INT_EN    bit 0: seek/tune complete
  +0x034 INT_CLR   write 1 to clear INT_STS bit 0; reads 0
  +0x038 INT_STS   bit 0: seek/tune complete (set whether or not enabled)
  +0x05c STER_PWR  bits 8:0 stereo threshold, dBm (stock 0x1a6 = -90)
  +0x0ac SEEK_CNT  channels the last seek examined
  +0x0b0 RSSI_STS  bits 7:0 RSSI (dBm, signed), bit 16 seek/tune failed,
                   bit 17 stereo, bits 19:18 pilot (01 = present here; the
                   real encoding is not known)
  +0x0b8 CHAN_FREQ channel being received, kHz
  +0x0bc FREQ_OFF  frequency offset: 0 (stations sit on their channel)
  +0x0c0 INPWR     bits 8:0 input power, dBm, 9-bit two's complement; the
                   driver reports 512 - (v & 0x1ff) as the signal (kern
                   0x04018044), so smaller is stronger
  +0x0c4 RF_RSSI   0: its scale is unknown and nothing uses it
  +0x0cc           bit 16 reads 1 (a calibration done flag, kern 0x04019070)
  +0x0d0 WBRSSI    bits 7:0 wideband RSSI, dBm, signed (same as RSSI here);
                   also the result byte of the boot-time ADC calibration
                   (kern 0x04018306), which reads it with the receiver
                   stopped and so gets 0, as before this model
  +0x0e0           bits 28 and 24 read 1: ADC calibration done (kern
                   0x04018300, 0x0401870c)
  Other offsets (AGC table +0x400.., coefficients +0x100..) are storage.
Status registers (+0x0ac..+0x0d0) read 0 until the first tune or seek
completes. They describe CHAN_FREQ, and the levels are live: a station
is heard only while EN bit 31 is set, the clock is on, the RF part is
powered and the antenna (the headset cable) is plugged in; otherwise the
levels are the noise floor.

Tune and seek (kern 0x040176ea, 0x0401785e): the driver clears EN bit 31,
sets STI, CTRL bit 19, COEF and CHAN, clears its done flag, sets INT_EN =
1 and then EN bit 31, and busy-waits (kern 0x040176b8, no timeout) for its
interrupt handler (kern 0x04017354, registered for line 44 at kern
0x04017486) to set the flag. The handler checks INT_STS bit 0, takes
!(RSSI_STS bit 16) as the result, then INT_EN &= 0xe and INT_CLR |= 1.
Line 44 is INT_STS bit 0 AND INT_EN bit 0, so the handler's INT_EN write
drops it and INT_CLR clears the status behind it.
  - A tune completes at once: CHAN_FREQ = CHAN; RSSI_STS bit 16 is set if
    there is no receivable station there. The real tune time is not known;
    the boot-time calibration (kern 0x04018fea) polls INT_STS with no
    timeout, so it is satisfied either way.
  - A seek examines channels from CHAN (inclusive) in steps of the spacing,
    in the direction of CTRL bit 19, wrapping between the band edges, at
    most SEEK_LOOP of them, and stops on the first receivable station
    (bit 16 clear) or gives up on the last channel examined (bit 16 set).
    It completes SEEK_DWELL_MS per channel examined after EN bit 31 is
    set. The stock seek (kern 0x04017e78) retries from where it stopped up
    to four times, so it covers 80 channels; its band scan (kern
    0x040178de) seeks up until CHAN_FREQ goes down (the band wrapped).
  A receivable station is one at least SEEK_MIN_DBM at the antenna. What
  SEEK_CH_TH (+0x040, stock 3000) measures is not known, so the model does
  not use it.

RF registers (0x8d000000, one word; kern 0x040172e0 write, 0x040172fa read):
  write (reg << 16) | value           sets 16-bit RF register reg
  write (reg << 16) | 0x80000000      selects reg for reading; the word then
                                      reads (reg << 16) | value
Every stock write is checked by reading it back (kern 0x0401732c).
RF 0x44 bits 15:12 are the RF part's power: 5 at power-up (kern
0x04017458), 0 at power-down; the model hears nothing while they are 0.

Every power, tune, seek and interrupt change is logged to <out>/fm.log.
"""

FM_BASE = 0x88002000
FM_SIZE = 0x1000
RF_PORT = 0x8d000000
APB_EB0, FM_EB = 0x8b0000a8, 1 << 29
APB_RST0, FM_SOFT_RST = 0x8b000068, 1 << 23
IRQ_LINE = 44

CTRL, EN, CHAN, SEEK_LOOP, STI = 0x000, 0x004, 0x00c, 0x014, 0x018
BAND_LOW, BAND_HIGH = 0x01c, 0x020
INT_EN, INT_CLR, INT_STS = 0x030, 0x034, 0x038
STER_PWR = 0x05c
SEEK_CNT, RSSI_STS, CHAN_FREQ, FREQ_OFF, INPWR, RF_RSSI = 0x0ac, 0x0b0, 0x0b8, 0x0bc, 0x0c0, 0x0c4
CAL_STS, WBRSSI, ADC_STS = 0x0cc, 0x0d0, 0x0e0
STATUS = (SEEK_CNT, RSSI_STS, CHAN_FREQ, FREQ_OFF, INPWR, RF_RSSI, WBRSSI)

CTRL_SEEK_UP = 1 << 19
EN_GO = 1 << 31
STI_PLAY, STI_SEEK, STI_TUNE = 0, 1, 2
INT_DONE = 1 << 0
RSSI_FAIL, RSSI_STEREO, RSSI_PILOT = 1 << 16, 1 << 17, 1 << 18
CAL_DONE = 1 << 16                      # +0x0cc
ADC_DONE = 1 << 28 | 1 << 24            # +0x0e0
FREQ_MASK = 0x1ffff
RF_READ = 1 << 31
RF_POWER_REG, RF_POWER_SHIFT = 0x44, 12

BLOCKS_PER_MS = 4000
SEEK_DWELL_MS = 5           # per channel a seek examines (real figure unknown)
SEEK_MIN_DBM = -85          # weakest station a tune or seek accepts
NOISE_DBM = -110            # what the receiver reads with no station

# The synthetic band: (kHz, dBm at the antenna, stereo). Stations sit
# exactly on their channel. 95.8 MHz is the frequency saved in this
# phone's settings, so the Radio app opens on a station; 100.4 MHz is too
# weak for a seek to stop on but can be tuned by hand; 104.9 MHz passes the
# stock seek's extra check above 104 MHz (kern 0x04017f28: INPWR above
# -80 dBm). The largest gap between receivable stations is 47 channels, so
# one stock seek (80 channels) always finds the next one.
BAND = (
    (88100, -62, True),
    (91300, -79, False),
    (95800, -55, True),
    (98000, -70, True),
    (100400, -88, False),
    (102700, -66, True),
    (104900, -74, True),
    (107600, -82, False),
)
STATIONS = {khz: (dbm, stereo) for khz, dbm, stereo in BAND}


def mhz(khz):
    return f"{khz / 1000:.2f} MHz"


def signed(value, bits):
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


class FmReceiver:
    def __init__(self, machine, log_path):
        self.m = machine
        self.log_file = open(log_path, "w")
        self.regs = {}          # offset -> value: the block's own registers
        self.rf = {}            # RF register -> 16-bit value
        self.rf_word = 0        # what the RF port reads
        self.freq = None        # kHz being received, after the first tune or seek
        self.failed = False     # RSSI_STS bit 16 of the last tune or seek
        self.seek_count = 0
        self.pending = 0        # generation of the seek in flight, 0 = none
        self.generation = 0
        self.line = False
        self.clock = False
        self.reset = False
        self.tunes = self.seeks = self.interrupts = 0

    def log(self, text):
        self.log_file.write(f"{self.m.nblocks:10d} {text}\n")

    def close(self):
        self.log_file.close()

    # clock and reset

    def stopped(self):
        return not self.clock or self.reset

    def on_clock(self, value):
        on = bool(value & FM_EB)
        if on != self.clock:
            self.clock = on
            self.log(f"clock {'on' if on else 'off'} (APB_EB0 bit 29)")
            if not on:
                self.abandon("clock off")

    def on_reset(self, value):
        held = bool(value & FM_SOFT_RST)
        if held != self.reset:
            self.reset = held
            self.log(f"reset {'held' if held else 'released'} (APB_RST0 bit 23)")
            if held:
                self.abandon("reset")
                self.regs.clear()
                self.freq = None
                self.failed = False
                self.seek_count = 0
                self.update_line()

    def abandon(self, why):
        if self.pending:
            self.log(f"seek abandoned: {why}")
            self.pending = 0

    # digital block

    def reg(self, off):
        return self.regs.get(off, 0)

    def read(self, off):
        if self.stopped():
            return 0
        if off == INT_CLR:
            return 0
        if off == CAL_STS:
            return self.reg(off) | CAL_DONE
        if off == ADC_STS:
            return self.reg(off) | ADC_DONE
        if off in STATUS:
            return self.status(off)
        return self.reg(off)

    def write(self, off, value):
        """Returns what the register now holds (stored back into m.regs so
        sub-word writes merge with it)."""
        if self.stopped():
            self.log(f"write +{off:03x} <- {value:08x} dropped: block stopped")
            return 0
        old = self.reg(off)
        if off == INT_CLR:
            if value & INT_DONE:
                self.set_done(False)
            return 0
        if off == INT_STS or off in STATUS:
            return self.read(off)       # read-only
        self.regs[off] = value
        if off == INT_EN:
            self.update_line()
        elif off == EN:
            if value & EN_GO and not old & EN_GO:
                self.start()
            elif old & EN_GO and not value & EN_GO:
                self.abandon("EN bit 31 cleared")
        return value

    def set_done(self, done):
        if done:
            self.regs[INT_STS] = self.reg(INT_STS) | INT_DONE
        else:
            self.regs[INT_STS] = self.reg(INT_STS) & ~INT_DONE
        self.update_line()

    def update_line(self):
        level = bool(self.reg(INT_STS) & self.reg(INT_EN) & INT_DONE)
        if level != self.line:
            self.line = level
            if level:
                self.interrupts += 1
            self.log(f"irq {IRQ_LINE} {'raised' if level else 'lowered'}")
        self.m.intc.set_line(IRQ_LINE, level)

    # tune and seek

    def band(self):
        """(bottom, top, spacing) in kHz, from +0x01c and +0x020."""
        top = self.reg(BAND_HIGH)
        return self.reg(BAND_LOW) & FREQ_MASK, top & FREQ_MASK, top >> 24

    def start(self):
        mode = self.reg(STI) & 3
        chan = self.reg(CHAN) & FREQ_MASK
        if mode == STI_TUNE:
            self.tunes += 1
            self.freq = chan
            self.failed = not self.receivable(chan)
            self.seek_count = 0
            self.log(f"tune {mhz(chan)}: {self.describe(chan)}")
            self.set_done(True)
        elif mode == STI_SEEK:
            self.seeks += 1
            self.generation += 1
            self.pending = self.generation
            found, last, count = self.seek(chan, bool(self.reg(CTRL) & CTRL_SEEK_UP))
            self.m.schedule(count * SEEK_DWELL_MS * BLOCKS_PER_MS,
                            lambda g=self.generation: self.seek_done(g, found, last, count))
        else:
            self.freq = chan
            self.failed = False
            self.log(f"play {mhz(chan)} (STI {mode}, no interrupt): {self.describe(chan)}")

    def seek(self, start, up):
        """Channels from start, as the hardware would examine them: returns
        (found, last channel examined, channels examined)."""
        low, high, step = self.band()
        limit = max(1, self.reg(SEEK_LOOP) & 0x3ff)
        wrap = low < high
        f = start
        for count in range(1, limit + 1):
            if wrap and f > high:
                f = low
            elif wrap and f < low:
                f = high
            if self.receivable(f):
                return True, f, count
            if count < limit:
                f = f + step if up else f - step
        return False, f, limit

    def seek_done(self, generation, found, last, count):
        if generation != self.pending:
            return
        self.pending = 0
        start = self.reg(CHAN) & FREQ_MASK
        direction = "up" if self.reg(CTRL) & CTRL_SEEK_UP else "down"
        self.freq = last
        self.failed = not found
        self.seek_count = count
        result = f"found {mhz(last)}, {self.describe(last)}" if found else f"nothing, stopped at {mhz(last)}"
        self.log(f"seek {direction} from {mhz(start)}: {count} channels, {result}")
        self.set_done(True)

    # reception

    def antenna(self):
        """The headset cable is the antenna."""
        return self.m.eic.headset

    def rf_powered(self):
        return self.rf.get(RF_POWER_REG, 0) >> RF_POWER_SHIFT & 0xf != 0

    def listening(self):
        return (not self.stopped() and self.reg(EN) & EN_GO
                and self.rf_powered() and self.antenna())

    def level(self, khz):
        """dBm the receiver measures on channel khz right now."""
        if khz in STATIONS and self.listening():
            return STATIONS[khz][0]
        return NOISE_DBM

    def receivable(self, khz):
        return self.level(khz) >= SEEK_MIN_DBM

    def stereo(self, khz):
        threshold = signed(self.reg(STER_PWR), 9)
        return khz in STATIONS and STATIONS[khz][1] and self.level(khz) >= threshold

    def describe(self, khz):
        dbm = self.level(khz)
        if khz in STATIONS and dbm != NOISE_DBM:
            kind = "stereo" if self.stereo(khz) else "mono"
            weak = "" if dbm >= SEEK_MIN_DBM else ", too weak"
            return f"station {dbm} dBm {kind}{weak}"
        why = []
        if not self.antenna():
            why.append("no antenna (headset)")
        if not self.rf_powered():
            why.append("RF off")
        return f"no station ({NOISE_DBM} dBm{': ' + ', '.join(why) if why else ''})"

    def status(self, off):
        if self.freq is None:
            return 0
        f = self.freq
        dbm = self.level(f)
        if off == CHAN_FREQ:
            return f & FREQ_MASK
        if off == SEEK_CNT:
            return self.seek_count & 0x3ff
        if off == INPWR:
            return dbm & 0x1ff
        if off == WBRSSI:
            return dbm & 0xff
        if off == RSSI_STS:
            v = dbm & 0xff
            if self.failed:
                v |= RSSI_FAIL
            if self.stereo(f):
                v |= RSSI_STEREO | RSSI_PILOT
            return v
        return 0        # FREQ_OFF, RF_RSSI

    # RF registers

    def read_rf(self):
        return self.rf_word

    def write_rf(self, value):
        reg = value >> 16 & 0x7fff
        if not value & RF_READ:
            was = self.rf_powered()
            self.rf[reg] = value & 0xffff
            if self.rf_powered() != was:
                self.log(f"rf {'on' if self.rf_powered() else 'off'} "
                         f"(RF 0x44 = {self.rf[RF_POWER_REG]:04x})")
        self.rf_word = reg << 16 | self.rf.get(reg, 0)

    def report(self):
        return f"fm: {self.tunes} tunes, {self.seeks} seeks, {self.interrupts} interrupts"


def install(read_handlers, write_handlers):
    """Route the block, the RF port and the clock/reset bits to m.fm."""
    for off in range(0, FM_SIZE, 4):
        read_handlers[FM_BASE + off] = lambda m, o=off: m.fm.read(o)
        write_handlers[FM_BASE + off] = (
            lambda m, v, o=off: m.regs.__setitem__(FM_BASE + o, m.fm.write(o, v)))
    read_handlers[RF_PORT] = lambda m: m.fm.read_rf()
    write_handlers[RF_PORT] = lambda m, v: m.fm.write_rf(v)
    write_handlers[APB_EB0] = lambda m, v: m.fm.on_clock(v)
    write_handlers[APB_RST0] = lambda m, v: m.fm.on_reset(v)
