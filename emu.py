#!/usr/bin/env python3
"""Stage 1 emulator for the Nokia 105 TA-1423 (UNISOC SC6531E).

Runs the boot ROM (or the flash image directly) on an ARM926 core in
Unicorn. Peripherals are a plain register file: reads return the last
value written (or a known value), and every access is logged. The run
stops with a report on an unmapped access, a register polling loop, a
CPU exception, or when the instruction budget runs out.
"""
import argparse
import collections
import ctypes
import datetime
import hashlib
import os
import signal
import struct
import sys
import threading
import time
import traceback
from pathlib import Path

import capstone
from unicorn import (UC_ARCH_ARM, UC_HOOK_BLOCK, UC_HOOK_CODE, UC_HOOK_INTR,
                     UC_HOOK_MEM_UNMAPPED, UC_MEM_FETCH_UNMAPPED,
                     UC_MEM_WRITE_UNMAPPED, UC_MODE_ARM, UC_PROT_ALL,
                     UC_PROT_READ, UC_PROT_WRITE, UC_PROT_EXEC, Uc, UcError)
from unicorn.arm_const import UC_CPU_ARM_926
import unicorn.arm_const as arm
from unicorn.unicorn_py3.unicorn import uc_hook_h, uclib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arm_exc  # noqa: E402
import audio  # noqa: E402
import battery  # noqa: E402
import dsp  # noqa: E402
import eic  # noqa: E402
import fm  # noqa: E402
import irq  # noqa: E402
import jpeg_hw  # noqa: E402
import keypad  # noqa: E402
import lcd  # noqa: E402
import lights  # noqa: E402
import lzma_engine  # noqa: E402
import mt_call  # noqa: E402
import sms_cnf  # noqa: E402
import mt_sms  # noqa: E402
import pmic  # noqa: E402
import sfc  # noqa: E402
import sim  # noqa: E402

ROOT = Path(__file__).resolve().parent


def default_dump():
    """$NOKIA_DUMP, else ./dump next to this file, else ../dump."""
    if os.environ.get("NOKIA_DUMP"):
        return Path(os.environ["NOKIA_DUMP"])
    return ROOT / "dump" if (ROOT / "dump").is_dir() else ROOT.parent / "dump"

CLOCK_LIB = ROOT / ("libblockclock.dylib" if sys.platform == "darwin" else "libblockclock.so")


class Clock(ctypes.Structure):
    """struct clock in blockclock.c, shared with the native block hook."""
    _fields_ = [("nblocks", ctypes.c_uint64), ("deadline", ctypes.c_uint64),
                ("last_addr", ctypes.c_uint64), ("irq_pending", ctypes.c_uint32),
                ("reason", ctypes.c_uint32), ("spin", ctypes.c_uint32),
                ("spin_limit", ctypes.c_uint32), ("cpsr_id", ctypes.c_int32),
                ("blocks_per_ms", ctypes.c_uint32), ("emu_stop", ctypes.c_void_p),
                ("reg_read", ctypes.c_void_p), ("counters", ctypes.c_void_p),
                ("next_ms", ctypes.c_uint64), ("next_tick", ctypes.c_uint64)]


STOP_DEADLINE, STOP_IRQ, STOP_SPIN = 1, 2, 4
# System counter page: +0x04 32 kHz ticks, +0x0c milliseconds (both read-only
# counts of emulated time); +0x00/+0x08 are plain storage. In fast mode it is
# host memory kept current by blockclock.c instead of MMIO.
COUNTER_PAGE = 0x81003000
PROGRESS_BLOCKS = 2_000_000
PACE_BLOCKS = 10 * 4000   # real-time mode: check the wall clock every 10 ms
# fast boot: unpaced until the boot has drawn this many screens (the third
# is the home screen), a key press, or this much emulated time
FAST_BOOT_SCREENS, FAST_BOOT_MAX_MS = 3, 20_000
AUTOSAVE_MS = 2000        # --state: save this long after the flash was last written

BOOTROM_SIZE = 0x16000
FLASH_BASE = 0x10000000   # memory-mapped SPI flash (custom FDL: FW_ADDR)
PSRAM_BASE = 0x04000000   # 4 MB (fphelper: RAM size = 4MB)
PSRAM_SIZE = 0x00400000
IRAM_BASE = 0x40000000    # internal SRAM (FDL bss/text/stack live here)
IRAM_SIZE = 0x00010000

# Bit 0 is the remap: the boot ROM's last act is writing 0 here, which puts
# flash at address 0. Bit 1 must be set for a normal boot: with it clear
# both the boot ROM and the flash bootloader (0x988) skip the boot-key
# check and go to download mode. Bit 2 is an unknown strap.
REMAP_REG = 0x205000e0

MMIO_REGIONS = [
    (0x20000000, 0x01000000),  # AHB peripherals (SFC at 0x20a00000)
    (0x30000000, 0x00010000),  # 16-bit stores (0x16868); probably DSP shared memory
    (0x69000000, 0x00001000),  # SPI1, drives the LCD (fpdoom sys_data.spi = 0x69 << 24)
    (0x80000000, 0x10000000),  # APB peripherals (global regs at 0x8b000000)
    (0x90000000, 0x00100000),  # USB controller
]

# Values from real hardware, taken from the custom FDL's chip detection.
KNOWN_REGS = {
    0x205003fc: 0x00000000,  # SC6530/SC6531DA chip ID; zero on SC6531E
    0x8b00035c: 0x36320000,  # SC6531E chip ID words
    0x8b000360: 0x53433635,
    0x8b000364: 0x00000001,
    REMAP_REG: 0x00000002,   # bit 0 is filled in by set_remap()
}

# Emulated time advances with executed blocks so runs are reproducible.
BLOCKS_PER_MS = 4000  # ~20 MIPS at ~5 instructions per block; the chip runs ~208 MHz

# Registers whose reads are computed rather than stored.
# 0x8100300c is the millisecond counter fpdoom's sys_timer_ms() reads;
# 0x81003004 is polled by the boot ROM's delay loop, rate guessed at 32 kHz.
#
# ADI is the bus to the analog/PMIC die (protocol from fpdoom's syscode.c):
# writes go straight to 0x82001xxx and wait on FIFO_STS; reads write the
# offset to RD_CMD and poll RD_DATA until bit 31 clears, getting the
# offset back in the high half and the register value in the low half.
ADI_FIFO_STS = 0x82000004
ADI_RD_CMD = 0x82000008
ADI_RD_DATA = 0x8200000c
ADI_ANA_BASE = 0x82001000
ADI_FIFO_EMPTY = 1 << 21


def adi_read_data(m):
    offset = m.regs.get(ADI_RD_CMD, 0) & 0xfff
    addr = ADI_ANA_BASE + offset
    handler = ANA_READ_HANDLERS.get(addr)
    value = handler(m) if handler else m.regs.get(addr, 0)
    return offset << 16 | (value & 0xffff)


# Analog (PMIC) registers, read through the ADI.
ANA_KNOWN_REGS = {
    0x82001400: 0xa000,   # ANA_CHIP_ID_LOW  (fpdoom; SC1161 on a TA-1575)
    0x82001404: 0x1161,   # ANA_CHIP_ID_HIGH
    # RTC spare register: high byte 0xbb marks the RTC as having kept time
    # (flash 0x1727c); otherwise boot resets the clock to 2023-01-01 (kern
    # 0x4004d6a). Nothing in a boot writes it, so it is taken to be kept
    # in the battery-backed domain from an earlier run.
    0x820011d0: 0xbb00,
}

# 0x82001080..0x820010a8 calibrates the 32.768 kHz clock (inferred from
# 0x17e0a): 0x17bf6 programs N into 0x82001088:0x82001084 and starts it,
# 0x17c4c waits for bit 0 of 0x820010a8 and reads 0x82001090:0x8200108c,
# and 0x17c90 divides 6.5 MHz-scaled constants by that result. Reading it
# as 6.5 MHz ticks over N + 1 slow cycles, a perfect crystal gives
# (N + 1) * 6500000 / 32768.
def clk32k_cal_ticks(m):
    n = (m.regs.get(0x82001088, 0) & 0xff) << 16 | (m.regs.get(0x82001084, 0) & 0xffff)
    return (n + 1) * 6_500_000 // 32768


def codec_status(value):
    if value & 1 << 15:
        value |= 1 << 12 | 1 << 10
    if value & 0x1a0 == 0x1a0:
        value |= 1 << 4 | 1 << 3
    return value


ANA_READ_HANDLERS = {
    0x820010a8: lambda m: m.regs.get(0x820010a8, 0) | 1,
    0x8200108c: lambda m: clk32k_cal_ticks(m) & 0xffff,
    0x82001090: lambda m: clk32k_cal_ticks(m) >> 16,
    # 0x17d56 writes 1 and waits for bit 0 to self-clear (busy)
    0x82001094: lambda m: m.regs.get(0x82001094, 0) & ~1,
    # audio codec (0x82001380..): the power-on sequence (kern 0x402b476)
    # writes 0x6006 then 0xe006 to +0x54 and sleeps 10 ms at a time until
    # bits 12 and 10 are set (taken as calibration done); started by bit 15.
    # A second sequence (kern 0x402b538, first used by the alarm) sets bits
    # 8, 5 and 7 and polls, with no timeout, for bits 4 and 3.
    0x820013d4: lambda m: codec_status(m.regs.get(0x820013d4, 0)),
    # ADC on the PMIC (flash 0x17a64 adc_read): channel in 0x82001304 bits 0-4
    # (scale in bits 5-6), done flag 0x8200135c bit 0, 12-bit result in
    # 0x8200134c. Conversions finish instantly; see adc_result.
    0x8200135c: lambda m: m.regs.get(0x8200135c, 0) | 1,
    0x8200134c: lambda m: adc_result(m),
    battery.CHG_STATUS: lambda m: m.battery.status(),
    pmic.RTC_INT_STS: lambda m: m.pmic.rtc_status,
    **{a: (lambda m, i=i: m.pmic.rtc_read(i)) for i, a in enumerate(pmic.RTC_COUNT)},
    pmic.ANA_INT_STS: lambda m: m.pmic.read_ana_int(),
    pmic.EIC_BASE + pmic.EIC_RIS: lambda m: m.pmic.eic_raw(),
    pmic.EIC_BASE + pmic.EIC_MIS: lambda m: m.pmic.eic_masked(),
    # EIC debounce data: one bit per analog input line.
    0x82001200: lambda m: m.eic_inputs(),
    # power-control write protect: 0x10dd0 writes the 0x6e7f key and waits
    # for exactly 0x8000 (bit 15 = unlocked), as on other Spreadtrum PMICs
    0x82001588: lambda m: 0x8000 if m.regs.get(0x82001588) == 0x6e7f else 0,
}


G2D_BASE = 0x20200000
G2D_CTRL = G2D_BASE
G2D_DONE = 1 << 7

READ_HANDLERS = {
    battery.EFUSE_DATA: lambda m: battery.efuse_read(m),
    0x81003004: lambda m: m.nblocks * 32768 // (1000 * BLOCKS_PER_MS),
    0x8100300c: lambda m: m.nblocks // BLOCKS_PER_MS,
    lzma_engine.LZMA_BASE + lzma_engine.INT: lambda m: m.lzma.read_int(),
    lzma_engine.LZMA_BASE + lzma_engine.CTRL_RB: lambda m: m.lzma.read_ctrl(),
    # a hardware state machine (flash 0x1f720): setting bit 25 of 0x8b000274
    # must bring bits 10-14 of 0x8b000278 to 7, clearing it brings them to 0;
    # timeouts raise errors 0xeaea0002/0xeaea0001. Purpose unknown.
    0x8b000278: lambda m: (m.regs.get(0x8b000278, 0) & ~(0x1f << 10)) | (
        7 << 10 if m.regs.get(0x8b000274, 0) & (1 << 25) else 0),
    # the FM receiver at 0x88002000 and its RF port: fm.py (installed below)
    dsp.SHARED_CMD: lambda m: m.dsp.read_shared_cmd(),  # see dsp.py
    # control register of the JPEG decoder (jpeg_hw.py): the MMI (user
    # 0x82817a2) writes 0x19 to 0x20200000 and waits for bit 7 = done;
    # writes are logged to g2d.log (Machine.on_g2d_start)
    G2D_CTRL: lambda m: m.regs.get(G2D_CTRL, 0) | G2D_DONE,
    jpeg_hw.BAND_SEL: lambda m: m.jpeg.read_sel(),
    jpeg_hw.INT_STS: lambda m: m.jpeg.int_raw,
    # image/display-processing block at 0x20210000 (the MMI loads tone-curve
    # tables at +0x884..): after starting it through +0x400/+0x404 (bit 31)
    # the MMI polls bit 31 of +0x418 for completion (user 0x8110b20)
    0x20210418: lambda m: m.regs.get(0x20210418, 0) | 1 << 31,
    # unidentified block at 0x20700000: kern 0x40803e4..0x408043c set command
    # bits 24-26 of +0x2c and wait for the hardware to clear them; kern
    # 0x4080242 sets bit 0 (enable) and waits for bit 1 (ready)
    0x2070002c: lambda m: (m.regs.get(0x2070002c, 0) & ~0x07000002) | (
        (m.regs.get(0x2070002c, 0) & 1) << 1),
    lcd.SPI1_BASE + lcd.TXD: lambda m: m.spi1.read_txd(),
    lcd.SPI1_BASE + lcd.INT_RAW: lambda m: m.regs.get(lcd.SPI1_BASE + lcd.INT_RAW, 0) | lcd.INT_TX_END,
    lcd.SPI1_BASE + lcd.STS2: lambda m: lcd.STS2_TXF_EMPTY,
    lcd.LCDC_BASE + lcd.IRQ_RAW: lambda m: m.lcdc.read_raw(),
    lcd.LCDC_BASE + lcd.IRQ_STATUS: lambda m: m.lcdc.read_status(),
    ADI_FIFO_STS: lambda m: ADI_FIFO_EMPTY,
    ADI_RD_DATA: adi_read_data,
    keypad.KPD_BASE + keypad.INT_RAW: lambda m: m.keypad.raw,
    keypad.KPD_BASE + keypad.INT_MASK: lambda m: m.keypad.read_masked(),
    keypad.KPD_BASE + keypad.KEY_STATUS: lambda m: m.keypad.status,
    eic.EIC_BASE + eic.DATA: lambda m: m.eic.level(),
    eic.EIC_BASE + eic.RIS: lambda m: m.eic.raw(),
    eic.EIC_BASE + eic.MIS: lambda m: m.eic.masked(),
    audio.DMA_INT_STS: lambda m: m.dma.read_status(),
    audio.AUD_STS: lambda m: m.aud.read_status(),
}

EIC_POWER_KEY = 1 << 1
POWERED_OFF = "powered off"     # stop reason when the phone switches itself off
SWITCHES = ("charger", "headset")   # --keys names that plug something in
EIC_BATTERY = 1 << 8  # inferred, see Machine.eic_inputs
EIC_HEADSET = 1 << 4  # TA-1557 headset detect, see Machine.eic_inputs

# ADC results are keyed by the channel register's low 7 bits (channel in
# bits 0-4, bit 5 set by adc_read's scale argument 0, bit 6 always set as
# observed), falling back to the channel number alone.
# 0x62 (channel 2): battery voltage read 7 times by the boot check at flash
#   0x1bf1a; with this emulator's (default) calibration the firmware computes
#   mV = 690 + 1.25 * raw, measured at raw 0x400/0x800/0xc00. Channels 2 and
#   3 (the charger voltage) come from battery.Battery unless --adc sets them.
# 0x60 (channel 0): read 12 times by the charge manager's periodic check
#   (flash 0x1b18e/0x1bece) and converted to a value that falls as raw rises
#   (0x20 -> 1166, 0x100 -> 1070, 0x400 -> 1023, 0x800 -> 1001; raw 0 is
#   special and gives 723). The check at flash 0x1b636 accepts 985..1058;
#   at either end it shows one of two messages (0x400003f0 high,
#   0x400003f1 low) and powers off 3 s later. The two-sided limit suggests
#   battery temperature (NTC) rather than voltage; that reading is inferred.
#   Values measured by running the firmware's conversion.
ADC_BATTERY_TEMP = 0x60
BATTERY_TEMP_RAW = 0x400
DEFAULT_BATTERY_MV = 3900


def adc_result(m):
    reg = m.regs.get(0x82001304, 0) & 0x7f
    channel = reg & 0x1f
    if reg in m.args.adc or channel in m.args.adc:
        raw = m.args.adc.get(reg, m.args.adc.get(channel))
    elif channel in (battery.ADC_VBAT, battery.ADC_VCHG):
        raw = m.battery.adc(channel)
    else:
        raw = 0
    return raw & 0xfff

# Registers whose writes trigger device behaviour (called with the full word).
WRITE_HANDLERS = {
    sfc.SFC_BASE + sfc.SOFT_REQ: lambda m, v: m.sfc.on_soft_req(v),
    lzma_engine.LZMA_BASE + lzma_engine.START: lambda m, v: m.lzma.on_start(v),
    lcd.SPI1_BASE + lcd.TXD: lambda m, v: m.spi1.on_txd(v),
    dsp.SHARED_CMD: lambda m, v: m.dsp.on_shared_cmd(v),
    G2D_CTRL: lambda m, v: m.on_g2d_start(v),
    jpeg_hw.START: lambda m, v: m.jpeg.on_start(v),
    jpeg_hw.BAND_START: lambda m, v: m.jpeg.on_band_start(v),
    jpeg_hw.INT_CLR: lambda m, v: m.jpeg.on_int_clr(v),
    jpeg_hw.INT_EN: lambda m, v: m.jpeg.update_line(),
    jpeg_hw.CONV_START: lambda m, v: m.jpeg.on_conv_start(v),
    dsp.INT_CLR: lambda m, v: m.dsp.on_int_clr(v),
    pmic.RTC_INT_CLR: lambda m, v: m.pmic.on_rtc_clr(v),
    pmic.EIC_BASE + pmic.EIC_IEV: lambda m, v: m.pmic.update(),
    pmic.EIC_BASE + pmic.EIC_IE: lambda m, v: m.pmic.update(),
    **{a: (lambda m, v, i=i: m.pmic.on_rtc_set(i, v)) for i, a in enumerate(pmic.RTC_SET)},
    **{a: (lambda m, v, i=i: m.pmic.on_alarm_set(i, v)) for i, a in enumerate(pmic.RTC_ALARM)},
    **{a: (lambda m, v: m.lights.on_write()) for a in lights.REGISTERS},
    pmic.POWER_DOWN: lambda m, v: m.power_off(),
    battery.CHG_CTRL: lambda m, v: m.battery.on_ctrl(),
    lcd.LCDC_BASE + lcd.LCDC_CTRL: lambda m, v: m.lcdc.on_ctrl(v),
    lcd.LCDC_BASE + lcd.IRQ_CLR: lambda m, v: m.lcdc.on_clr(v),
    lcd.LCDC_BASE + lcd.IRQ_EN: lambda m, v: m.lcdc.update_line(),
    lzma_engine.LZMA_BASE + lzma_engine.INT: lambda m, v: m.lzma.on_int_write(v),
    sfc.SFC_BASE + sfc.TBUF_CLR: lambda m, v: m.sfc.on_tbuf_clr(v),
    sfc.SFC_BASE + sfc.INT_CLR: lambda m, v: m.sfc.on_int_clr(v),
    keypad.KPD_BASE + keypad.INT_CLR: lambda m, v: m.keypad.on_clr(v),
    keypad.KPD_BASE + keypad.INT_EN: lambda m, v: m.keypad.update_line(),
    eic.EIC_BASE + eic.IEV: lambda m, v: m.eic.update(),
    eic.EIC_BASE + eic.IE: lambda m, v: m.eic.update(),
    eic.EIC_BASE + eic.IC: lambda m, v: m.eic.on_clr(v),
    **{a: (lambda m, v, ch=ch: m.vbc.on_fifo_write(ch, v)) for ch, a in enumerate(audio.VBC_DA)},
    audio.AUD_CLR: lambda m, v: m.aud.on_clear(v),
    audio.AUD_EN: lambda m, v: m.aud.on_enable(v),
}

ARM_EXCEPTIONS = {1: "undefined instruction", 2: "SWI", 3: "prefetch abort",
                  4: "data abort", 5: "IRQ", 6: "FIQ", 7: "BKPT"}


for _bank, _base in enumerate(irq.INTC_BANKS):
    for _off in (irq.IRQ_STS, irq.IRQ_RAW, irq.IRQ_EN):
        READ_HANDLERS[_base + _off] = lambda m, b=_bank, o=_off: m.intc.read(b, o)
    for _off in (irq.IRQ_EN, irq.IRQ_DIS):
        WRITE_HANDLERS[_base + _off] = lambda m, v, b=_bank, o=_off: m.intc.write(b, o, v)
for _ch in range(audio.CHANNELS):
    _base = audio.CH_BASE + audio.CH_STRIDE * _ch
    READ_HANDLERS[_base + audio.PAUSE] = lambda m, ch=_ch: m.dma.read_pause(ch)
    READ_HANDLERS[_base + audio.INTC] = lambda m, ch=_ch: m.dma.read_intc(ch)
    WRITE_HANDLERS[_base + audio.CFG] = lambda m, v, ch=_ch: m.dma.on_cfg(ch, v)
    WRITE_HANDLERS[_base + audio.INTC] = lambda m, v, ch=_ch: m.dma.on_intc(ch, v)
fm.install(READ_HANDLERS, WRITE_HANDLERS)
for _i, _base in enumerate(irq.TIMER_BASES):
    READ_HANDLERS[_base + irq.VALUE] = lambda m, i=_i: m.timers[i].value()
    READ_HANDLERS[_base + irq.INT] = lambda m, i=_i: m.timers[i].read_int()
    WRITE_HANDLERS[_base + irq.CTL] = lambda m, v, i=_i: m.timer_ctl(i, v)
    WRITE_HANDLERS[_base + irq.INT] = lambda m, v, i=_i: m.timers[i].on_int(v)


class Machine:
    def __init__(self, args):
        self.args = args
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.uc.ctl_set_cpu_model(UC_CPU_ARM_926)
        self.md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)

        self.regs = dict(KNOWN_REGS)
        self.regs.update(ANA_KNOWN_REGS)
        self.stats = collections.defaultdict(lambda: [0, 0, None, None, None])
        self.polls = {}
        self.blocks = collections.deque(maxlen=48)
        self.coverage = set()
        self.region = None
        self.clock = Clock()      # nblocks lives here, see the property
        # the native block hook (blockclock.c) unless per-block Python work
        # is asked for (--slow, -v, --trace-blocks)
        self.fast = not (args.slow or args.verbose or args.trace_blocks != (0, -1))
        self.spin = 0
        self.next_progress = PROGRESS_BLOCKS
        self.next_pace = None
        self.stop_reason = None
        self.trace = open(args.out / "mmio_trace.log", "w")
        self.trace_lines = 0
        self.errors = open(args.out / "errors.log", "w")
        self._buffers = []

        self._map_memory()
        self.flash_log = open(args.out / "flash.log", "w")
        self.sfc = sfc.Sfc(self, sfc.SpiFlash(
            memoryview(self.flash).cast("B"), self.flash_size,
            log=lambda line: self.flash_log.write(f"{self.nblocks:10d} {line}\n"),
            on_change=self.on_flash_change, now=lambda: self.nblocks,
            schedule=lambda delay, cb: self.schedule(delay, cb),
            blocks_per_ms=BLOCKS_PER_MS, on_busy=self.set_flash_busy))
        self.lzma = lzma_engine.LzmaEngine(self)
        self.blocks_per_ms = BLOCKS_PER_MS
        self.intc = irq.Intc(self)
        self.timers = [irq.Timer(self, i) for i in range(len(irq.TIMER_BASES))]
        self.next_timer = None
        self.events = []        # (block, callback) scheduled by device models
        self.wfi_count = 0      # wait-for-interrupt sleeps skipped over
        self.wfi_blocks = 0     # emulated time skipped while asleep, in blocks
        self.dsp = dsp.Dsp(self)
        self.pmic = pmic.Pmic(self, args.rtc_time)
        # the power key is let go after --power-key-ms: the EIC sees the change
        self.schedule(args.power_key_ms * BLOCKS_PER_MS, self.pmic.update)
        self.eic = eic.DigitalEic(self, args.headset)
        self.fm = fm.FmReceiver(self, args.out / "fm.log")
        self.vbc = audio.Vbc(self, args.out / "audio.wav")
        self.dma = audio.Dma(self, self.vbc)
        self.aud = audio.AudCtl(self)
        # the SIM controller is only modelled when a card is inserted, so a
        # run without --sim sees the registers exactly as before
        self.sim = None
        if args.sim:
            self.sim = sim.SimController(self, {args.sim_slot}, args.out / "sim.log")
            sim.install(self, READ_HANDLERS, WRITE_HANDLERS)
        self.inject_sms = mt_sms.install(self, args.inject_sms, BLOCKS_PER_MS) if args.inject_sms else None
        self.inject_call = (mt_call.install(self, args.inject_call, BLOCKS_PER_MS, args.inject_call_event)
                            if args.inject_call or args.inject_call_event or args.log_calls
                            or args.fail_startcall is not None else None)
        self.sms_cnf = (sms_cnf.install(self, args.sms_send_ok, BLOCKS_PER_MS)
                        if args.sms_send_ok or args.log_sms_cnf else None)
        self.keypad = keypad.Keypad(self, args.dump / "unpacked" / "keymap.bin",
                                    log=lambda line: print(f"[block {self.nblocks}] {line}"))
        self.lights = lights.Lights(self, log=lambda line: print(f"[block {self.nblocks}] {line}"))
        mv = args.battery_mv or self.saved_battery() or DEFAULT_BATTERY_MV
        self.battery = battery.Battery(self, mv, args.charger, args.charge_minutes,
                                       log=lambda line: print(f"[block {self.nblocks}] {line}"))
        for at_ms, name, hold_ms in args.keys:
            if name in SWITCHES:        # plugged in for HOLD ms
                self.schedule(at_ms * BLOCKS_PER_MS, lambda n=name: self.switch(n, True))
                self.schedule((at_ms + hold_ms) * BLOCKS_PER_MS, lambda n=name: self.switch(n, False))
                continue
            if name not in self.keypad.names():
                raise SystemExit(f"--keys: unknown key {name!r}; known: "
                                 f"{' '.join(self.keypad.names() + list(SWITCHES))}")
            self.schedule(at_ms * BLOCKS_PER_MS, lambda n=name: self.keypad.press(n))
            self.schedule((at_ms + hold_ms) * BLOCKS_PER_MS, lambda n=name: self.keypad.release(n))
        # input from another thread (live.py): (press|release, key name)
        # or (headset, plugged)
        self.inputs = collections.deque()
        self.input_ready = threading.Event()
        # pace emulated time to the wall clock while the guest sleeps
        self.realtime = False
        self.wall_start = None
        # real-time mode: run the boot unpaced while it is silent
        self.fast_boot = False
        self.fast_boot_input = False    # a key was pressed: the boot is over
        self.was_pacing = False
        self.run_start = None   # wall time the run started
        self.lcd_log = open(args.out / "lcd.log", "w")
        self.g2d_log = open(args.out / "g2d.log", "w")
        self.g2d_ops = 0
        self.jpeg = jpeg_hw.JpegDecoder(self, log=lambda line: self.g2d_log.write(
            f"{self.nblocks:10d} jpeg {line}\n"))
        self.panel = lcd.Panel(args.lcd_id, log=lambda line: self.lcd_log.write(
            f"{self.nblocks:10d} {line}\n"))
        self.spi1 = lcd.Spi1(self, self.panel)
        self.lcdc = lcd.Lcdc(self, self.spi1, args.out / "frames")
        self.exc_log = open(args.out / "exceptions.log", "w")
        self.exc = arm_exc.ExceptionEntry(self.uc, log=print)
        # the latest exception and how many times in a row it has repeated
        self.last_exc, self.exc_repeats = None, 0
        if self.fast:
            self._install_clock()
            for addr in args.breaks:
                self.uc.hook_add(UC_HOOK_CODE, self._guard(self._on_break), begin=addr, end=addr)
        else:
            self.uc.hook_add(UC_HOOK_BLOCK, self._guard(self._on_block))
        self.update_deadline()
        self.uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._guard(self._on_unmapped, False))
        self.uc.hook_add(UC_HOOK_INTR, self._guard(self._on_intr))

    @property
    def nblocks(self):
        """Emulated time in translation blocks (BLOCKS_PER_MS per ms)."""
        return self.clock.nblocks

    @nblocks.setter
    def nblocks(self, value):
        self.clock.nblocks = value

    def _install_clock(self):
        """Register blockclock.c's hook through Unicorn's C API."""
        if not CLOCK_LIB.exists():
            raise SystemExit(f"{CLOCK_LIB.name} not found: run `make` in {ROOT} first")
        self._clock_lib = ctypes.CDLL(str(CLOCK_LIB))
        c = self.clock
        c.spin_limit = self.args.poll_limit
        c.cpsr_id = arm.UC_ARM_REG_CPSR
        c.blocks_per_ms = BLOCKS_PER_MS
        c.counters = ctypes.addressof(self.counter_page)
        c.emu_stop = ctypes.cast(uclib.uc_emu_stop, ctypes.c_void_p).value
        c.reg_read = ctypes.cast(uclib.uc_reg_read, ctypes.c_void_p).value
        self._clock_hook = uc_hook_h()
        err = uclib.uc_hook_add(self.uc._uch, ctypes.byref(self._clock_hook), UC_HOOK_BLOCK,
                                ctypes.cast(self._clock_lib.clock_block_hook, ctypes.c_void_p),
                                ctypes.c_void_p(ctypes.addressof(c)), 1, 0)
        if err:
            raise SystemExit(f"uc_hook_add failed: error {err}")

    def update_deadline(self):
        """Tell the native hook when Python next has work: the next timer
        expiry, device event, progress line, pacing check or the budget."""
        due = [self.next_progress, self.args.max_blocks]
        if self.next_timer is not None:
            due.append(self.next_timer)
        if self.events:
            due.append(self.events[0][0])
        if self.next_pace is not None:
            due.append(self.next_pace)
        self.clock.deadline = min(due)

    def _guard(self, fn, default=None):
        """Wrap a Unicorn callback so a Python error stops the run with its
        traceback in errors.log (the binding would otherwise swallow it)."""
        def wrapper(*args):
            try:
                return fn(*args)
            except Exception:
                if self.stop_reason is None:
                    self.errors.write(f"block {self.nblocks} pc {self._pc():08x}\n"
                                      f"{traceback.format_exc()}\n")
                    self.stop(f"python error in {fn.__name__}, see errors.log")
                return default
        return wrapper

    # memory

    def _map_file(self, base, path, size=None, perms=UC_PROT_ALL):
        data = path.read_bytes()
        size = size or (len(data) + 0xfff) & ~0xfff
        buf = ctypes.create_string_buffer(data, size)
        self._buffers.append(buf)
        self.uc.mem_map_ptr(base, size, perms, ctypes.addressof(buf))
        return buf, size

    def _flash_source(self):
        """The flash image to boot: the saved state (--state) when it was made
        from this dump, else the dump itself."""
        dump = self.args.dump / "flash.bin"
        self.dump_hash = hashlib.sha256(dump.read_bytes()).hexdigest()
        state = self.args.state
        if state and state.exists():
            tag = state.with_name(state.name + ".source")
            if tag.exists() and tag.read_text().strip() == self.dump_hash:
                print(f"flash: saved state {state}")
                return state
            print(f"flash: {state} was saved from a different dump, starting from the dump")
        return dump

    def on_flash_change(self):
        self.flash_changed_at = self.nblocks

    def autosave(self):
        """With --state, save once the firmware has left the flash alone for
        AUTOSAVE_MS (it writes a settings change at once, in a burst of
        programs and erases), so a crash or force-quit loses at most that."""
        if (self.args.state and self.flash_changed_at is not None
                and self.nblocks - self.flash_changed_at >= AUTOSAVE_MS * BLOCKS_PER_MS):
            self.flash_changed_at = None
            self.save_state()

    def save_state(self):
        """Write the flash back to --state if the firmware changed it since
        the last save (its settings, contacts and messages live in flash)."""
        state = self.args.state
        data = bytes(self.flash)[:self.flash_size]
        digest = hashlib.sha256(data).digest()
        if not state or digest == self.saved_hash:
            return
        self.saved_hash = digest
        state.parent.mkdir(parents=True, exist_ok=True)
        tmp = state.with_name(state.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(state)
        state.with_name(state.name + ".source").write_text(self.dump_hash + "\n")
        print(f"flash: saved state to {state}")

    def _map_memory(self):
        self.flash, self.flash_size = self._map_file(
            FLASH_BASE, self._flash_source(), perms=UC_PROT_READ | UC_PROT_EXEC)
        self.saved_hash = hashlib.sha256(bytes(self.flash)[:self.flash_size]).digest()
        self.flash_changed_at = None    # block count of the latest unsaved flash write
        self.bootrom = ctypes.create_string_buffer(
            (self.args.dump / "bootrom.bin").read_bytes(), BOOTROM_SIZE)
        # what XIP sees while the NOR chip is busy (sfc.py, Busy timing): the
        # W25Q32 datasheet leaves reads during a program/erase/status write
        # undefined; all 0xFF is the conservative, deterministic choice
        self.flash_ff = ctypes.create_string_buffer(b"\xff" * self.flash_size, self.flash_size)
        self.flash_busy = False
        self.rom_at_zero = None
        self.set_remap(self.args.start == "bootrom")
        self.uc.mem_map(PSRAM_BASE, PSRAM_SIZE)
        self.uc.mem_map(IRAM_BASE, IRAM_SIZE)
        regions = list(MMIO_REGIONS)
        if self.fast:
            self.counter_page = ctypes.create_string_buffer(0x1000)
            self.uc.mem_map_ptr(COUNTER_PAGE, 0x1000, UC_PROT_READ | UC_PROT_WRITE,
                                ctypes.addressof(self.counter_page))
            regions = []
            for base, size in MMIO_REGIONS:
                if base <= COUNTER_PAGE < base + size:
                    regions += [(base, COUNTER_PAGE - base),
                                (COUNTER_PAGE + 0x1000, base + size - COUNTER_PAGE - 0x1000)]
                else:
                    regions.append((base, size))
        for base, size in regions:
            self.uc.mmio_map(base, size, self._guard(self._mmio_read, 0), base,
                             self._guard(self._mmio_write), base)

    def set_remap(self, rom_at_zero):
        """Bit 0 of REMAP_REG: 1 = boot ROM at address 0, 0 = flash at 0."""
        if rom_at_zero == self.rom_at_zero:
            return
        if self.rom_at_zero is not None:
            self.uc.mem_unmap(0, BOOTROM_SIZE if self.rom_at_zero else self.flash_size)
            print(f"[block {self.nblocks}] remap: "
                  f"{'boot ROM' if rom_at_zero else 'flash'} now at address 0")
        if rom_at_zero:
            self.uc.mem_map_ptr(0, BOOTROM_SIZE, UC_PROT_READ | UC_PROT_EXEC,
                                ctypes.addressof(self.bootrom))
        else:
            self.uc.mem_map_ptr(0, self.flash_size, UC_PROT_READ | UC_PROT_EXEC,
                                ctypes.addressof(self.flash_ff if self.flash_busy else self.flash))
        self.rom_at_zero = rom_at_zero
        self.regs[REMAP_REG] = (self.regs.get(REMAP_REG, 0) & ~1) | int(rom_at_zero)

    def set_flash_busy(self, busy):
        """The NOR chip went busy (True) or ready (False): every XIP window
        onto it (FLASH_BASE, and address 0 when the flash is remapped there)
        is switched between the image and the all-0xFF buffer, and the
        translated code from those windows is dropped so instruction fetches
        see the change at once (Unicorn's translation cache would otherwise
        act as an unlimited I-cache)."""
        if busy == self.flash_busy:
            return
        self.flash_busy = busy
        src = ctypes.addressof(self.flash_ff if busy else self.flash)
        spans = [FLASH_BASE] + ([0] if self.rom_at_zero is False else [])
        for base in spans:
            self.uc.mem_unmap(base, self.flash_size)
            self.uc.mem_map_ptr(base, self.flash_size, UC_PROT_READ | UC_PROT_EXEC, src)
            self.uc.ctl_remove_cache(base, base + self.flash_size)

    def schedule(self, delay_blocks, callback):
        self.events.append((self.nblocks + delay_blocks, callback))
        self.events.sort(key=lambda e: e[0])
        self.update_deadline()

    def run_events(self):
        while self.events and self.events[0][0] <= self.nblocks:
            _, callback = self.events.pop(0)
            callback()
        self.update_deadline()

    def timer_ctl(self, i, value):
        self.timers[i].on_ctl(value)
        self.reschedule_timers()

    def reschedule_timers(self):
        due = [t.expiry_block() for t in self.timers]
        due = [d for d in due if d is not None]
        self.next_timer = min(due) if due else None
        self.update_deadline()

    def run_timers(self):
        for t in self.timers:
            due = t.expiry_block()
            if due is not None and due <= self.nblocks:
                t.tick()
        self.reschedule_timers()

    def on_g2d_start(self, value):
        if not value & 1:
            return
        self.g2d_ops += 1
        if self.g2d_ops <= 200:
            regs = " ".join(f"{o:02x}={self.regs.get(G2D_BASE + o, 0):08x}"
                            for o in range(0, 0x80, 4) if self.regs.get(G2D_BASE + o))
            self.g2d_log.write(f"{self.nblocks:10d} op {self.g2d_ops} pc {self._pc():08x}: {regs}\n")

    def inject(self, action, arg):
        """Queue a key press/release, a headset or charger plug change or a
        battery voltage; safe to call from another thread."""
        self.inputs.append((action, arg))
        self.input_ready.set()
        self.clock.deadline = 0     # stop at the next block to take it
        if action == "press":
            self.fast_boot_input = True

    def take_inputs(self):
        while self.inputs:
            action, arg = self.inputs.popleft()
            if action in SWITCHES:
                self.switch(action, arg)
            elif action == "battery":
                self.battery.set_mv(arg)
            else:
                (self.keypad.press if action == "press" else self.keypad.release)(arg)

    def switch(self, name, plugged):
        """Plug the headset or the charger in or out."""
        if name == "charger":
            self.battery.plug(plugged)
        else:
            self.eic.set_headset(plugged)
            print(f"[block {self.nblocks}] headset {'plugged in' if plugged else 'unplugged'}")

    def battery_path(self):
        return self.args.state.with_name(self.args.state.name + ".battery") if self.args.state else None

    def saved_battery(self):
        """The battery voltage when the saved phone last ran, if any."""
        path = self.battery_path()
        if path is None:
            return None
        try:
            return float(path.read_text())
        except (OSError, ValueError):
            return None

    def save_battery(self):
        path = self.battery_path()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{self.battery.mv:.0f}\n")

    def wall_blocks(self):
        """Wall-clock time since the run started, in blocks."""
        return int((time.monotonic() - self.wall_start) * 1000 * BLOCKS_PER_MS)

    def sleep_until(self, due):
        """Real-time mode: wait while the emulated clock is ahead of the wall
        clock. Returns early (with the clock moved to now) on queued input."""
        if self.wall_start is None:
            self.wall_start = time.monotonic() - self.nblocks / (1000 * BLOCKS_PER_MS)
        # time lost while the guest was busy is not made up later
        behind = self.wall_blocks() - self.nblocks
        if behind > 100 * BLOCKS_PER_MS:
            self.wall_start += (behind - 100 * BLOCKS_PER_MS) / (1000 * BLOCKS_PER_MS)
        while not self.inputs and self.stop_reason is None:
            ahead = due - self.wall_blocks()
            if ahead <= 0:
                return due
            self.input_ready.wait(min(ahead / (1000 * BLOCKS_PER_MS), 0.25))
            self.input_ready.clear()
        return min(due, max(self.nblocks, self.wall_blocks()))

    def pacing(self):
        """Whether emulated time is held to the wall clock now: in real-time
        mode, except during a fast boot while no sound is playing (sound
        has to play at its own pace). Each return to pacing starts the wall
        clock from here, so that time skipped is not waited for."""
        if not self.realtime:
            return False
        if self.fast_boot and (self.fast_boot_input or self.lcdc.refreshes >= FAST_BOOT_SCREENS
                or self.nblocks >= FAST_BOOT_MAX_MS * BLOCKS_PER_MS):
            self.end_fast_boot()
        pacing = not self.fast_boot or self.vbc.sounding()
        if pacing and not self.was_pacing:
            self.wall_start = time.monotonic() - self.nblocks / (1000 * BLOCKS_PER_MS)
        self.was_pacing = pacing
        return pacing

    def end_fast_boot(self):
        """The boot ran ahead of the wall clock: set the RTC back by as much,
        so that it shows the real time again."""
        self.fast_boot = False
        emulated = self.nblocks / (1000 * BLOCKS_PER_MS)
        wall = time.monotonic() - self.run_start
        self.pmic.shift(-round(emulated - wall))
        print(f"[block {self.nblocks}] fast boot: {emulated:.1f} s in {wall:.1f} s")

    def power_key_held(self):
        return self.nblocks // BLOCKS_PER_MS < self.args.power_key_ms

    def eic_inputs(self):
        """EIC debounce data (0x82001200). Bit 1 is the power key (fpdoom
        keypad_read_pb; the power-on reason check at flash 0x1f0c6 reads it).
        Bit 8 is kernel input 0x18: a normal boot powers off if it reads 0
        (kern 0x4000b5a), so it is taken to be battery detect. Bit 0 is the
        charger (battery.py). Bit 4 is the TA-1557's headset detect, high
        when plugged in: with it set the status bar shows the headset icon
        and Radio stops asking for one. (That phone never reads the digital
        EIC, where the TA-1423 has its headset, eic.py; --headset sets both.)"""
        bits = 0
        if self.eic.headset:
            bits |= EIC_HEADSET
        if self.power_key_held() or self.keypad.power_held:
            bits |= EIC_POWER_KEY
        if self.battery.plugged:
            bits |= battery.EIC_CHARGER
        if not self.args.no_battery:
            bits |= EIC_BATTERY
        return bits

    def read_span(self, addr, length=None):
        """Read length bytes, or up to the end of the mapping holding addr."""
        if length is None:
            end = next(e for b, e, _ in self.uc.mem_regions() if b <= addr <= e)
            length = end + 1 - addr
        return bytes(self.uc.mem_read(addr, length))

    def region_of(self, addr):
        if addr < 0x400000:
            if self.rom_at_zero:
                return "bootrom" if addr < BOOTROM_SIZE else "?"
            return "flash@0"
        for name, base, size in (("flash", FLASH_BASE, 0x400000),
                                 ("psram", PSRAM_BASE, PSRAM_SIZE),
                                 ("iram", IRAM_BASE, IRAM_SIZE)):
            if base <= addr < base + size:
                return name
        return "?"

    # hooks

    def _pc(self):
        return self.uc.reg_read(arm.UC_ARM_REG_PC)

    def _tracing(self, addr):
        lo, hi = self.args.trace_addrs
        return (self.trace_lines < self.args.trace_limit and lo <= addr <= hi
                and self.nblocks >= self.args.trace_after)

    def _log(self, line):
        self.trace.write(line + "\n")
        self.trace_lines += 1

    def _mmio_read(self, uc, offset, size, base):
        addr = base + offset
        handler = READ_HANDLERS.get(addr & ~3)
        word = handler(self) & 0xffffffff if handler else self.regs.get(addr & ~3, 0)
        value = word >> (8 * (addr & 3))
        value &= (1 << (8 * size)) - 1
        st = self.stats[addr]
        st[0] += 1
        if st[2] is None:
            st[2] = self._pc()
        st[4] = value
        if self._tracing(addr):
            self._log(f"{self.nblocks:10d} {self._pc():08x} R{size} {addr:08x} -> {value:08x}")
        # a poll only counts while the value stays the same (timers move)
        count, last = self.polls.get(addr, (0, None))
        count = count + 1 if value == last else 1
        self.polls[addr] = (count, value)
        if count == self.args.poll_limit:
            self.stop(f"polling loop: pc {self._pc():08x} read {addr:08x} "
                      f"{self.args.poll_limit} times (value {value:#x})")
        return value

    def _mmio_write(self, uc, offset, size, value, base):
        addr = base + offset
        self.polls.clear()  # a poll only counts reads with no writes between
        word = addr & ~3
        shift = 8 * (addr & 3)
        mask = ((1 << (8 * size)) - 1) << shift
        old = self.regs.get(word, 0)
        self.regs[word] = (old & ~mask) | ((value << shift) & mask)
        if word == REMAP_REG:
            # only the remap bit is writable; the boot straps are read-only
            self.regs[word] = (old & ~1) | (self.regs[word] & 1)
            self.set_remap(bool(self.regs[word] & 1))
        handler = WRITE_HANDLERS.get(word)
        if handler:
            handler(self, self.regs[word])
        st = self.stats[addr]
        st[1] += 1
        if st[2] is None:
            st[2] = self._pc()
        st[3] = value
        if self._tracing(addr):
            self._log(f"{self.nblocks:10d} {self._pc():08x} W{size} {addr:08x} <- {value:08x}")

    def _on_block(self, uc, addr, size, _):
        self.nblocks += 1
        if self.nblocks >= self.args.max_blocks:
            self.stop(f"block budget ({self.args.max_blocks}) used up")
        if self.next_timer is not None and self.nblocks >= self.next_timer:
            self.run_timers()
        if self.events and self.nblocks >= self.events[0][0]:
            self.run_events()
        if self.inputs:
            self.take_inputs()
        if self.intc.pending() and not uc.reg_read(arm.UC_ARM_REG_CPSR) & arm_exc.CPSR_I:
            self.intc.note_delivery()
            self.exc.deliver(arm_exc.EXC_IRQ, pc=addr)
            return
        lo, hi = self.args.trace_blocks
        if lo <= self.nblocks <= hi:
            r = [uc.reg_read(getattr(arm, f"UC_ARM_REG_R{i}")) for i in range(4)]
            cpsr = uc.reg_read(arm.UC_ARM_REG_CPSR)
            print(f"  blk {self.nblocks} {addr:08x}{' T' if cpsr & 0x20 else ' A'} "
                  f"lr={uc.reg_read(arm.UC_ARM_REG_LR):08x} "
                  f"r0-r3={' '.join(f'{v:08x}' for v in r)}")
        if self.blocks and self.blocks[-1] == addr:
            self.spin += 1
            if self.spin == self.args.poll_limit:
                self.stop(f"spinning: block {addr:08x} re-entered itself "
                          f"{self.spin} times")
            return
        self.spin = 0
        self.blocks.append(addr)
        self.coverage.add(addr)
        if addr in self.args.breaks and self.nblocks >= self.args.break_after:
            self.stop(f"breakpoint at {addr:08x} (lr {uc.reg_read(arm.UC_ARM_REG_LR):08x})")
        if self.nblocks >= self.next_progress:
            self.progress(addr)
        region = self.region_of(addr) if self.args.verbose else self.region
        if region != self.region:
            print(f"[block {self.nblocks}] execution moved {self.region} -> "
                  f"{region} at {addr:08x}")
            self.region = region

    def progress(self, pc):
        self.next_progress = self.nblocks + PROGRESS_BLOCKS
        unique = len(self.coverage) if not self.fast else "n/a"
        print(f"[block {self.nblocks}] pc {pc:08x}, unique blocks {unique}, "
              f"emulated {self.nblocks // BLOCKS_PER_MS} ms, lcd pixels {self.panel.pixels}, "
              f"irqs {sum(self.intc.delivered.values())}, timer fires {[t.fires for t in self.timers]}")
        if self.panel.dirty:
            self.panel.save_png(self.args.out / "screen.png")
        self.update_deadline()

    def _on_break(self, uc, addr, size, _):
        if self.nblocks >= self.args.break_after:
            self.stop(f"breakpoint at {addr:08x} (lr {uc.reg_read(arm.UC_ARM_REG_LR):08x})")

    def service(self, reason, pc):
        """Work for the native hook's stops (blockclock.c); returns where to
        resume."""
        if reason & STOP_SPIN:
            self.stop(f"spinning: block {self.clock.last_addr:08x} re-entered itself "
                      f"{self.clock.spin} times")
            return pc
        if self.nblocks >= self.args.max_blocks:
            self.stop(f"block budget ({self.args.max_blocks}) used up")
            return pc
        if self.next_timer is not None and self.nblocks >= self.next_timer:
            self.run_timers()
        if self.events and self.nblocks >= self.events[0][0]:
            self.run_events()
        if self.inputs:
            self.take_inputs()
        if self.nblocks >= self.next_progress:
            self.progress(pc)
        if self.realtime and self.next_pace is not None and self.nblocks >= self.next_pace:
            if self.pacing():
                self.pace()
            else:
                self.next_pace = self.nblocks + PACE_BLOCKS
        if self.intc.pending() and not self.uc.reg_read(arm.UC_ARM_REG_CPSR) & arm_exc.CPSR_I:
            self.intc.note_delivery()
            self.exc.deliver(arm_exc.EXC_IRQ, pc=pc & ~1)
            pc = self._resume_pc()
        self.update_deadline()
        return pc

    def pace(self):
        """Real-time mode while the guest is busy: hold back when emulated
        time has run ahead of the wall clock."""
        if self.wall_start is None:
            self.wall_start = time.monotonic() - self.nblocks / (1000 * BLOCKS_PER_MS)
        ahead = (self.nblocks - self.wall_blocks()) / (1000 * BLOCKS_PER_MS)
        if ahead > 0 and not self.inputs:
            self.input_ready.wait(min(ahead, 0.05))
            self.input_ready.clear()
        self.next_pace = self.nblocks + PACE_BLOCKS

    def _resume_pc(self):
        return self._pc() | (1 if self.uc.reg_read(arm.UC_ARM_REG_CPSR) & arm_exc.CPSR_T else 0)

    def _on_unmapped(self, uc, access, addr, size, value, _):
        kind = {UC_MEM_FETCH_UNMAPPED: "fetch", UC_MEM_WRITE_UNMAPPED: "write"}.get(access, "read")
        if self.args.automap and access != UC_MEM_FETCH_UNMAPPED:
            page = addr & ~0xfff
            print(f"automap: {kind} {addr:08x} at pc {self._pc():08x}, mapping RAM page {page:08x}")
            uc.mem_map(page, 0x1000)
            return True
        self.stop(f"unmapped {kind} of {size} bytes at {addr:08x}")
        return False

    def _on_intr(self, uc, intno, _):
        pc = self._pc()
        what = self.exc.deliver(intno)
        if what is None:
            self.stop(f"CPU exception {intno} ({ARM_EXCEPTIONS.get(intno, '?')}) at {pc:08x}")
            return
        self.exc_log.write(f"{self.nblocks:10d} {what}\n")
        # A storm is the same exception back to back with nothing in between.
        # The same abort recurring over a session is normal: demand-paged
        # code and resources get evicted and faulted in again.
        if intno == arm_exc.EXC_SWI:
            return
        key = (intno, what)
        self.exc_repeats = self.exc_repeats + 1 if key == self.last_exc else 1
        self.last_exc = key
        if self.exc_repeats == 1000:
            self.stop(f"exception storm: {what} repeated 1000 times in a row")

    def stop(self, reason):
        if self.stop_reason is None:
            self.stop_reason = reason
        self.uc.emu_stop()

    def power_off(self):
        """The phone switched itself off: the run ends, as the chip loses
        power (the saved phone is written as usual)."""
        print(f"[block {self.nblocks}] power off")
        self.stop(POWERED_OFF)

    # run and report

    def run(self):
        # Unicorn returns from emu_start when the guest executes the ARM926
        # wait-for-interrupt (mcr p15, 0, rX, c7, c0, 4). Time here only moves
        # with executed blocks, so skip ahead to the next device event (which
        # raises the interrupt that would wake the CPU) and carry on.
        pc = 0
        self.run_start = time.monotonic()
        if self.realtime:
            self.next_pace = self.nblocks + PACE_BLOCKS
            self.update_deadline()
        while True:
            try:
                self.uc.emu_start(pc, 0xffffffff)
            except UcError as e:
                if self.stop_reason is None:
                    self.stop_reason = f"unicorn error: {e}"
            if self.stop_reason is not None:
                break
            pc = self._resume_pc()
            reason, self.clock.reason = self.clock.reason, 0
            if reason or self.clock.deadline == 0:
                pc = self.service(reason, pc)
                if self.stop_reason is not None:
                    break
                continue
            if not self.after_wfi(pc & ~1):
                self.stop_reason = f"emulation returned at {pc & ~1:08x} for no known reason"
                break
            if not self.wake_from_wfi():
                self.stop_reason = f"wait for interrupt at {pc & ~1:08x} with nothing scheduled to wake it"
                break
        self.trace.close()
        self.flash_log.close()
        self.save_state()
        self.save_battery()
        self.exc_log.close()
        self.errors.close()
        self.lcd_log.close()
        self.g2d_log.close()
        self.fm.close()
        self.vbc.close()
        self.panel.save_png(self.args.out / "screen.png")
        self.report()

    def after_wfi(self, pc):
        try:
            word = struct.unpack("<I", arm_exc.read_virt(self.uc, pc - 4, 4))[0]
        except UcError:
            return False
        return word & 0x0fff0fff == 0x0e070f90

    def wake_from_wfi(self):
        # a pending interrupt ends the sleep at once, even with IRQs masked
        if self.intc.pending():
            self.wfi_count += 1
            return True
        # otherwise only something that raises an interrupt can end it:
        # timers with their interrupt enabled, and scheduled device events
        due = [t.expiry_block() for t in self.timers if t.reg(irq.INT) & irq.INT_EN]
        due = [d for d in due if d is not None]
        if self.events:
            due.append(self.events[0][0])
        if not due and not self.realtime:
            return False
        until = min(due) if due else self.nblocks + 3_600_000 * BLOCKS_PER_MS
        if self.pacing():
            until = self.sleep_until(until)
        self.wfi_count += 1
        self.wfi_blocks += max(0, until - self.nblocks)
        self.nblocks = max(self.nblocks, until)
        self.take_inputs()
        if self.next_timer is not None and self.nblocks >= self.next_timer:
            self.run_timers()
        self.run_events()
        self.autosave()
        if self.realtime:
            self.next_pace = self.nblocks + PACE_BLOCKS
        self.update_deadline()
        return True

    def disasm_at(self, addr, n=6):
        thumb = self.uc.reg_read(arm.UC_ARM_REG_CPSR) & 0x20
        self.md.mode = capstone.CS_MODE_THUMB if thumb else capstone.CS_MODE_ARM
        try:
            code = arm_exc.read_virt(self.uc, addr, n * 4)
        except UcError:
            return ["  <unreadable>"]
        return [f"  {i.address:08x}: {i.mnemonic} {i.op_str}"
                for i in self.md.disasm(code, addr)][:n]

    def report(self):
        uc = self.uc
        out = []
        out.append(f"stop: {self.stop_reason}")
        unique = len(self.coverage) if not self.fast else "not tracked (use --slow)"
        out.append(f"blocks executed: {self.nblocks}, unique blocks: {unique}")
        names = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "cpsr"]
        ids = [getattr(arm, f"UC_ARM_REG_R{i}") for i in range(13)] + [
            arm.UC_ARM_REG_SP, arm.UC_ARM_REG_LR, arm.UC_ARM_REG_PC, arm.UC_ARM_REG_CPSR]
        vals = [uc.reg_read(r) for r in ids]
        out.append("regs: " + " ".join(f"{n}={v:08x}" for n, v in zip(names, vals)))
        for name, v in zip(names[:4], vals[:4]):
            try:
                words = struct.unpack("<8I", arm_exc.read_virt(uc, v & ~3, 32))
                out.append(f"  [{name}] " + " ".join(f"{w:08x}" for w in words))
            except UcError:
                pass
        out.append(f"irq: enabled {[hex(e) for e in self.intc.en]}, raw {[hex(r) for r in self.intc.raw]}, "
                   f"delivered {self.intc.delivered}; timer fires {[t.fires for t in self.timers]}; "
                   f"dsp interrupts {self.dsp.interrupts}; rtc events {self.pmic.rtc_events}; "
                   f"wfi sleeps {self.wfi_count} ({self.wfi_blocks // BLOCKS_PER_MS} ms skipped); "
                   f"key presses {self.keypad.presses}; audio: {self.dma.blocks} DMA blocks, "
                   f"{self.vbc.samples} samples; {self.fm.report()}")
        out.append(f"mmu: sctlr={arm_exc.cp15(uc, 1):08x} ttbr={arm_exc.cp15(uc, 2):08x} "
                   f"dacr={arm_exc.cp15(uc, 3):08x}; exceptions delivered: {self.exc.counts}")
        out.append("code at pc:")
        out += self.disasm_at(vals[15])
        last = self.blocks if not self.fast else [self.clock.last_addr]
        out.append("last blocks: " + " ".join(f"{a:08x}" for a in last))
        out.append("")
        out.append(f"peripheral registers touched: {len(self.stats)}")
        out.append("    addr      reads  writes  first_pc  last_write  last_read")
        for addr in sorted(self.stats):
            r, w, pc, lw, lr = self.stats[addr]
            fmt = lambda v: f"{v:08x}" if v is not None else "-"
            out.append(f"  {addr:08x} {r:8d} {w:7d}  {pc:08x}  {fmt(lw):>10}  {fmt(lr):>9}")
        text = "\n".join(out)
        (self.args.out / "report.txt").write_text(text + "\n")
        (self.args.out / "psram.bin").write_bytes(bytes(uc.mem_read(PSRAM_BASE, PSRAM_SIZE)))
        (self.args.out / "iram.bin").write_bytes(bytes(uc.mem_read(IRAM_BASE, IRAM_SIZE)))
        print(text if len(self.stats) < 80 else "\n".join(out[:12] + [
            f"... full register table in {self.args.out / 'report.txt'}"]))


def parse_keys(spec):
    """--keys MS:KEY[:HOLD_MS],... -> [(ms, key, hold_ms)]"""
    keys = []
    for item in filter(None, spec.split(",")):
        parts = item.split(":")
        keys.append((int(parts[0]), parts[1], int(parts[2]) if len(parts) > 2 else 150))
    return keys


def parse_args(argv=None, parser=None):
    p = parser or argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print every move between memory regions")
    p.add_argument("--start", choices=["bootrom", "flash"], default="bootrom",
                   help="reset into the boot ROM (default) or straight into flash")
    p.add_argument("--max-blocks", type=int, default=400_000_000,
                   help="stop after this many executed blocks (emulated time = blocks / %d per ms)" % BLOCKS_PER_MS)
    p.add_argument("--poll-limit", type=int, default=200_000,
                   help="stop when one pc reads one register this many times")
    p.add_argument("--trace-limit", type=int, default=None,
                   help="maximum lines in mmio_trace.log (default: 200000 with --trace-after or "
                        "--trace-addrs, else no trace)")
    p.add_argument("--trace-after", type=int, default=0, metavar="BLOCK",
                   help="start mmio_trace.log at this block count")
    p.add_argument("--trace-addrs", default="0:0xffffffff", metavar="LO:HI",
                   help="only log peripheral addresses in this range")
    p.add_argument("--power-key-ms", type=int, default=3000,
                   help="hold the power key for this long after reset (emulated ms)")
    p.add_argument("--no-battery", action="store_true",
                   help="report no battery on the battery-detect input (EIC 8)")
    p.add_argument("--lcd-id", type=lambda v: int(v, 0), default=0x83760f,
                   help="ID the LCD panel reports (default: first entry of the firmware's table)")
    p.add_argument("--battery-mv", type=int, default=None,
                   help=f"battery voltage at power-on (default: as the saved phone left it, "
                        f"else {DEFAULT_BATTERY_MV})")
    p.add_argument("--charger", action="store_true", help="start with the charger plugged in")
    p.add_argument("--charge-minutes", type=float, default=10,
                   help="time to charge the battery from empty to full (default 10)")
    p.add_argument("--adc", action="append", default=[], metavar="CH=RAW",
                   help="raw 12-bit ADC result: KEY is a channel (0-31) or a channel register value with scale bits (0x20-0x7f)")
    p.add_argument("--automap", action="store_true",
                   help="map unmapped data accesses as RAM instead of stopping")
    p.add_argument("--trace-blocks", default="0:-1", metavar="FIRST:LAST",
                   help="print every block in this block-count range")
    p.add_argument("--break", dest="breaks", default="", metavar="ADDR,...",
                   help="stop when a block starts at one of these addresses")
    p.add_argument("--break-after", type=int, default=0, metavar="BLOCK",
                   help="only honour --break once this many blocks have run")
    p.add_argument("--rtc-time", type=datetime.datetime.fromisoformat, default=datetime.datetime.now(),
                   metavar="YYYY-MM-DDTHH:MM:SS", help="RTC time at power-on (default: host local time)")
    p.add_argument("--headset", action="store_true", help="start with a headset plugged in")
    p.add_argument("--sim", action="store_true",
                   help="insert a test SIM card (GSM 11.11, see sim.py); commands go to OUT/sim.log")
    p.add_argument("--sim-slot", type=int, choices=(0, 1, 2), default=0,
                   help="slot the --sim card is in (0 = the phone's SIM1)")
    p.add_argument("--inject-sms", type=mt_sms.parse, action="append", default=[], metavar="MS:NUMBER:TEXT",
                   help="live MT text at emulated time MS (repeatable; needs --sim): stored in the SIM's first "
                        "free EF_SMS record, then APP_MN_SMS_IND 0xa1a7 dispatched on the APP task (mt_sms.py); "
                        "log in OUT/inject-sms.log")
    p.add_argument("--inject-call", type=mt_call.parse, action="append", default=[], metavar="MS:NUMBER[:ENDMS]",
                   help="live MT call at emulated time MS (repeatable): APP_MN_SETUP_IND 0xa15d with the caller's "
                        "digits dispatched on the APP task; at ENDMS (optional) APP_MN_DISCONNECTED_IND 0xa15b, "
                        "the far end hanging up (mt_call.py); log in OUT/inject-call.log")
    p.add_argument("--inject-call-event", type=mt_call.parse_event, action="append", default=[],
                   metavar="MS:KIND[:ARG]",
                   help="CC event for call_id 0 at emulated time MS (repeatable), dispatched like --inject-call: "
                        "alerting[:TYPE] 0xa158, connect 0xa159 (MT SETUP_COMPLETE_IND), connect:mo 0xa15a "
                        "(SETUP_COMPLETE_CNF), disc:CAUSE 0xa15b, err:CAUSE 0xa15c (mt_call.py); also logs "
                        "MNCALL_Start/Connect/DisconnectCallEx calls to OUT/inject-call.log")
    p.add_argument("--fail-startcall", type=int, default=None, metavar="RET",
                   help="MNCALL_StartCallEx 0x0823f04d refuses every call: it returns RET (non-zero) at once "
                        "without running (mt_call.py); logged to OUT/inject-call.log")
    p.add_argument("--sms-send-ok", action="store_true",
                   help="answer every MO SMS with APP_MN_SEND_SMS_CNF 0xa1a3 cause 0 (sent), as a real network "
                        "does; without it the networkless MN answers cause 2 (sms_cnf.py); log in OUT/sms-cnf.log")
    p.add_argument("--log-sms-cnf", action="store_true",
                   help="log each APP_MN_SEND_SMS_CNF 0xa1a3 (cause) to OUT/sms-cnf.log with no change")
    p.add_argument("--log-calls", action="store_true",
                   help="log MNCALL_StartCallEx / ConnectCallEx / DisconnectCallEx calls (name, args, lr, ms) to "
                        "OUT/inject-call.log with no injection (implied by --inject-call / --inject-call-event)")
    p.add_argument("--slow", action="store_true",
                   help="per-block Python hook (tracks unique blocks; implied by -v and --trace-blocks)")
    p.add_argument("--keys", type=parse_keys, default=[], metavar="MS:KEY[:HOLD],...",
                   help="press KEY at emulated time MS for HOLD ms (default 150); keys: "
                        "0-9 * # up down left right select lsoft rsoft call end")
    p.add_argument("--state", type=Path, default=None, metavar="FILE",
                   help="keep the phone's flash (settings, contacts, messages) in FILE: "
                        "boot from it if it exists, save changes to it at the end")
    p.add_argument("--dump", type=Path, default=default_dump(), metavar="DIR",
                   help="directory with flash.bin and bootrom.bin (default: $NOKIA_DUMP, ./dump or ../dump)")
    p.add_argument("--out", type=Path, default=ROOT / "out")
    args = p.parse_args(argv)
    for name in ("flash.bin", "bootrom.bin"):
        if not (args.dump / name).is_file():
            p.error(f"{args.dump / name} not found: dump your phone first (docs/DUMPING.md) or pass --dump DIR")
    args.breaks = {int(a, 0) for a in args.breaks.split(",") if a}
    args.adc = {ADC_BATTERY_TEMP: BATTERY_TEMP_RAW,
                **{int(k, 0): int(v, 0) for k, v in (a.split("=") for a in args.adc)}}
    if args.trace_limit is None:   # the MMIO trace is on when a filter asks for it
        args.trace_limit = 200_000 if args.trace_after or args.trace_addrs != "0:0xffffffff" else 0
    args.trace_addrs = tuple(int(x, 0) for x in args.trace_addrs.split(":"))
    args.trace_blocks = tuple(int(x, 0) for x in args.trace_blocks.split(":"))
    args.out.mkdir(exist_ok=True)
    return args


def main():
    machine = Machine(parse_args())
    # stop cleanly on kill/Ctrl-C so the report and memory dumps still get written
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, _: machine.stop(f"signal {signum}"))
    machine.run()


if __name__ == "__main__":
    sys.exit(main())
