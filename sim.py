"""SIM card interface (0x89000000, IRQ 17) and a GSM 11.11 SIM card behind it.

Controller
----------
One controller serves all the phone's SIM slots: bits 2-3 of APB_PIN_CTL0
(0x8b0001e0) choose which slot its pins are routed to (2013 Mocor
sc6531_reg_global.h SIM_SEL; this firmware writes it at kern 0x40077c8 and
tries slot 0 four times, then slot 1). Base and interrupt line are those of
the 2013 SC6531 headers (sc6531_reg_base.h SIM0_BASE = SIM1_BASE =
0x89000000, sc6531_int_cfg.h TB_SIM_INT = 0x11), and the kernel agrees:
SIMDRV_InitDriver (kern 0x4006fc4) builds the base as `movs r5, #0x89;
lsls r5, #0x18` (0x4006fd0) and stores it as the register map of both card
indexes (0x4007014, 0x4007070); it registers its handlers for interrupt
0x11 (`movs r0, #0x11` before the ISR_RegCallback / ISR_RegHandler_Ex calls
at 0x4006ff0, 0x4007004, 0x400704e, 0x4007062); and the ISR (kern
0x4006d9e) picks the card by testing bit 17 of the INTC status (0x4006dee).
SIMDRV_L_Switch (kern 0x40077c8) rewrites bits 2-3 of 0x8b0001e0. The
register layout is sim_reg_v5.h, which sc6531_module_ver.h includes; each
offset below is checked against the kernel driver (accessors at kern
0x4006c00..0x40078d6):
  +0x00 TX     write a byte into the TX FIFO           (kern 0x40070f8)
  +0x04 RX     read a byte from the RX FIFO             (kern 0x4006c00)
  +0x08 STS0   interrupt status                         (ISR 0x4006dc0)
  +0x0c STS1   pin status (bit 10 card in, 12 active; header only: the
               phone writes it, never reads it)
  +0x10 IE     interrupt enable                         (0x400708e, 0x4006d80)
  +0x14 ICLR   write 1s to clear STS0 bits              (ISR 0x4006e1e)
  +0x18 CTL0   bit 4/5 RX/TX FIFO reset, 6 RST pin, 7 power, 9 RX enable,
               10 TX enable, 15 force I/O low           (0x400741c, 0x40071de)
  +0x1c CTL1   bit 11 clock enable                      (0x40070ba/ce/e2)
  +0x20/+0x34  RX/TX clock dividers (the phone writes 0xba0; not modelled)
  +0x24 SHE, +0x28 TGC, +0x2c WDT, +0x30 IMSK, +0x38 WDT1  (stored only)
  +0x3c CTL2   bits 0-5 RX interrupt mark, 8-13 TX mark (0x400722c, 0x400738c)
  +0x40 STS2   bits 0-6 RX FIFO count, 8-14 TX FIFO count (0x4006d4c)
STS0/IE/ICLR bits: 0 RX full (count >= RX mark), 1 TX empty (count <= TX
mark), 2/3 RX/TX parity error, 4 card unresponsive, 5 card in, 6 card out,
7 early ATR, 8 activation done, 9 RX timeout. Bits 0 and 1 follow the FIFO
levels here; the rest latch until cleared. RX timeout is raised when bytes
sit in the RX FIFO below the mark and the card has nothing more to send,
which is what the driver's watchdog (WDT/WDT1) is for. The FIFOs hold 64
bytes (sim_reg_v5.h SIM_FIFO_BUF_SIZE).

Card
----
The card sees the I/O line only: it answers a rising RST edge on its slot
with an ATR, then speaks T=0 (ISO 7816-3) at the default F/D, one byte
entering the RX FIFO every BYTE_BLOCKS. Bytes the phone writes to TX go to
the card and are not echoed into the RX FIFO (the HAL's RX handler takes
the first byte after a header as the procedure byte, sim_hal.c
SIMHAL_RX_Handler).

ATR 3B 02 14 50: direct convention, T0 = 0x02 (no interface bytes, two
historical bytes). With no TA1 the phone keeps F = 372, D = 1 and sends no
PPS (sim_hal.c CheckReceiveBuf goes straight to TB_SIM_INSERTED_OK unless
TA1 asks for a faster rate); a PPS would also switch the 2013 HAL to DMA
transfers (sim_dma_support), which this model does not provide. No TB1
(programming voltage must be absent), no TC1 (extra guard time must be 0 or
255), no TD1 (T=0 only, so no TCK byte).

The card is a 2G GSM 11.11 SIM: class A0 only; any other class (a 3G phone
probing for a USIM with class 00) gets 6E 00, so the phone falls back to
2G. CHV1 is disabled, so no PIN is asked for. Files and their contents are
built in CardFiles; UPDATE commands change them for the rest of the run.
Every command and its answer is appended to <out>/sim.log.
"""
import collections

SIM_BASE = 0x89000000
TX, RX, STS0, STS1, IE, ICLR, CTL0, CTL1 = 0x00, 0x04, 0x08, 0x0c, 0x10, 0x14, 0x18, 0x1c
CTL2, STS2 = 0x3c, 0x40
IRQ_LINE = 17
PIN_CTL0 = 0x8b0001e0       # APB_PIN_CTL0; SIM_SEL in bits 2-3
FIFO_SIZE = 64

ST_RX_FULL, ST_TX_EMPTY, ST_RX_TOUT = 1 << 0, 1 << 1, 1 << 9
CTL0_RX_FIFO_RST, CTL0_TX_FIFO_RST, CTL0_RST, CTL0_POWER = 1 << 4, 1 << 5, 1 << 6, 1 << 7
CTL1_CLK_EN = 1 << 11
STS1_CARD_IN, STS1_ACTIVE = 1 << 10, 1 << 12

BLOCKS_PER_MS = 4000
ATR_DELAY_BLOCKS = 2 * BLOCKS_PER_MS      # RST high to TS: 400..40000 clocks
RESPONSE_DELAY_BLOCKS = BLOCKS_PER_MS     # header or data in to first byte out
BYTE_BLOCKS = 400                         # one byte every 0.1 ms
RX_TIMEOUT_BLOCKS = BLOCKS_PER_MS         # bytes below the RX mark, line idle

ATR = bytes.fromhex("3b021450")

# T=0 direction of each class-A0 instruction (GSM 11.11 section 9.2):
# data from the card (the phone reads P3 bytes), data to the card (the
# phone writes P3 bytes after the procedure byte), or no data.
CARD_TO_PHONE = {0xb0: "READ BINARY", 0xb2: "READ RECORD", 0xc0: "GET RESPONSE",
                 0xf2: "STATUS", 0x12: "FETCH"}
PHONE_TO_CARD = {0xa4: "SELECT", 0xd6: "UPDATE BINARY", 0xdc: "UPDATE RECORD",
                 0xa2: "SEEK", 0x32: "INCREASE", 0x20: "VERIFY CHV", 0x24: "CHANGE CHV",
                 0x26: "DISABLE CHV", 0x28: "ENABLE CHV", 0x2c: "UNBLOCK CHV",
                 0x88: "RUN GSM ALGORITHM", 0x10: "TERMINAL PROFILE", 0xc2: "ENVELOPE",
                 0x14: "TERMINAL RESPONSE"}
NO_DATA = {0x04: "INVALIDATE", 0x44: "REHABILITATE", 0xfa: "SLEEP"}

MF, DF, EF = 1, 2, 4
TRANSPARENT, LINEAR, CYCLIC = 0, 1, 3


class File:
    def __init__(self, fid, name, kind, parent=None, structure=TRANSPARENT,
                 data=b"", record_len=0, access=(0x11, 0x00, 0x44)):
        self.fid, self.name, self.kind, self.parent = fid, name, kind, parent
        self.structure, self.record_len, self.access = structure, record_len, access
        self.data = bytearray(data)
        self.children = []
        if parent:
            parent.children.append(self)

    def records(self):
        return len(self.data) // self.record_len if self.record_len else 0


def bcd_swapped(digits):
    """Digits packed two per byte, low nibble first, padded with F."""
    digits = digits + "f" * (len(digits) % 2)
    return bytes(int(digits[i + 1] + digits[i], 16) for i in range(0, len(digits), 2))


def imsi_bytes(imsi):
    """EF_IMSI (GSM 11.11 10.3.2): length, then digit 1 with the
    odd/even + type nibble (9 = odd, IMSI), then the rest swapped."""
    parity = 9 if len(imsi) % 2 else 1
    packed = bytes([int(imsi[0]) << 4 | parity]) + bcd_swapped(imsi[1:])
    return bytes([len(packed)]) + packed.ljust(8, b"\xff")


def address_bytes(number, size=12):
    """An SMS address field as stored on the SIM (length of the rest, TON/NPI,
    swapped BCD), padded with FF to `size` bytes."""
    intl = number.startswith("+")
    digits = number.lstrip("+")
    body = bytes([0x91 if intl else 0x81]) + bcd_swapped(digits)
    return (bytes([len(body)]) + body).ljust(size, b"\xff")


def alpha(text, size):
    return text.encode("ascii").ljust(size, b"\xff")


class CardFiles:
    """The card's file system: a 2G SIM with SMS, SMS parameters and ADN."""

    def __init__(self, imsi="001010123456789", smsc="+447000000000"):
        mf = self.mf = File(0x3f00, "MF", MF)
        File(0x2fe2, "EF_ICCID", EF, mf, data=bcd_swapped("89440000000000000001"),
             access=(0x0f, 0x00, 0xff))
        File(0x2f05, "EF_ELP", EF, mf, data=b"en" + b"\xff" * 4, access=(0x01, 0x00, 0x44))
        gsm = File(0x7f20, "DF_GSM", DF, mf)
        File(0x6f05, "EF_LP", EF, gsm, data=bytes([0x01, 0xff, 0xff, 0xff]))
        File(0x6f07, "EF_IMSI", EF, gsm, data=imsi_bytes(imsi), access=(0x14, 0x00, 0x44))
        File(0x6f20, "EF_KC", EF, gsm, data=b"\xff" * 8 + b"\x07")
        File(0x6f30, "EF_PLMNsel", EF, gsm, data=b"\xff" * 24)
        File(0x6f31, "EF_HPLMN", EF, gsm, data=bytes([0x0a]), access=(0x14, 0x00, 0x44))
        # services (2 bits each: allocated, activated): 1 CHV1 disable,
        # 2 ADN, 4 SMS, 7 PLMN selector, 9 MSISDN, 10 EXT1, 12 SMSP, 17 SPN
        File(0x6f38, "EF_SST", EF, gsm, data=bytes([0xcf, 0x30, 0xcf, 0x00, 0x03]),
             access=(0x14, 0x00, 0x44))
        File(0x6f46, "EF_SPN", EF, gsm, data=b"\x00" + alpha("EMU SIM", 16),
             access=(0x04, 0x00, 0x44))
        File(0x6f74, "EF_BCCH", EF, gsm, data=bytes(16))
        File(0x6f78, "EF_ACC", EF, gsm, data=bytes([0x02, 0x00]), access=(0x14, 0x00, 0x44))
        File(0x6f7b, "EF_FPLMN", EF, gsm, data=b"\xff" * 12)
        # TMSI, LAI, TMSI time, location update status 1 = not updated
        File(0x6f7e, "EF_LOCI", EF, gsm,
             data=b"\xff" * 4 + bytes.fromhex("00f110fffe") + b"\xff\x01")
        File(0x6fad, "EF_AD", EF, gsm, data=bytes([0x00, 0x00, 0x00, 0x02]),
             access=(0x04, 0x00, 0x44))
        File(0x6fae, "EF_PHASE", EF, gsm, data=bytes([0x03]), access=(0x04, 0x00, 0x44))
        # emergency numbers 112, 999 (3 bytes each, BCD swapped)
        File(0x6fb7, "EF_ECC", EF, gsm, data=bcd_swapped("112f") + b"\xff" + bcd_swapped("999f") + b"\xff",
             access=(0x04, 0x00, 0x44))
        tel = File(0x7f10, "DF_TELECOM", DF, mf)
        File(0x6f3a, "EF_ADN", EF, tel, LINEAR, b"\xff" * 28 * 20, 28)
        File(0x6f3b, "EF_FDN", EF, tel, LINEAR, b"\xff" * 28 * 10, 28, access=(0x12, 0x00, 0x44))
        File(0x6f3c, "EF_SMS", EF, tel, LINEAR, (b"\x00" + b"\xff" * 175) * 10, 176)
        File(0x6f40, "EF_MSISDN", EF, tel, LINEAR, b"\xff" * 28 * 2, 28)
        # SMS parameters (GSM 11.11 10.5.6): 12-byte alpha tag, indicators
        # (0 = present: SC address, PID, DCS, validity; no destination),
        # destination, SC address, PID 00, DCS 00, validity A7 (24 h)
        smsp = (alpha("SMSC", 12) + bytes([0xe1]) + b"\xff" * 12
                + address_bytes(smsc) + bytes([0x00, 0x00, 0xa7]))
        File(0x6f42, "EF_SMSP", EF, tel, LINEAR, smsp, len(smsp))
        # last TP-MR used, memory capacity flag (bit 0 set = space available)
        File(0x6f43, "EF_SMSS", EF, tel, data=bytes([0x00, 0xff]))
        File(0x6f4a, "EF_EXT1", EF, tel, LINEAR, b"\xff" * 13 * 10, 13)

    def all(self):
        stack = [self.mf]
        while stack:
            f = stack.pop()
            yield f
            stack.extend(f.children)


class Card:
    """A GSM 11.11 SIM speaking T=0: bytes in through receive(), bytes out
    through the `out` queue."""

    def __init__(self, files, log):
        self.files = files
        self.log = log
        self.out = collections.deque()
        self.reset_state()

    def reset_state(self):
        self.out.clear()
        self.active = False
        self.header = bytearray()
        self.data = bytearray()
        self.need = 0            # data bytes still expected after the header
        self.current_df = self.files.mf
        self.current_ef = None
        self.record = 0          # current record number (1-based, 0 = none)
        self.response = b""      # for GET RESPONSE
        self.last_select = ""

    def cold_reset(self):
        self.reset_state()
        self.active = True
        self.out.extend(ATR)
        self.log(f"reset: ATR {ATR.hex(' ')}")

    def deactivate(self):
        if self.active:
            self.log("deactivated")
        self.reset_state()

    def receive(self, byte):
        if not self.active:
            return
        if self.need:
            self.data.append(byte)
            self.need -= 1
            if not self.need:
                self.execute()
            return
        self.header.append(byte)
        if len(self.header) < 5:
            return
        cla, ins, p1, p2, p3 = self.header
        if cla != 0xa0:
            self.finish(b"", 0x6e00)
        elif ins in PHONE_TO_CARD and p3:
            self.need = p3
            self.out.append(ins)          # procedure byte: send all the data
        elif ins in PHONE_TO_CARD or ins in NO_DATA or ins in CARD_TO_PHONE:
            self.execute()
        else:
            self.finish(b"", 0x6d00)

    def execute(self):
        cla, ins, p1, p2, p3 = self.header
        try:
            data, sw = self.command(ins, p1, p2, p3, bytes(self.data))
        except Exception as e:     # keep the phone going; the log says why
            self.log(f"model error {e!r}")
            data, sw = b"", 0x6f00
        if ins in CARD_TO_PHONE and data:
            le = p3 or 256
            if len(data) != le:
                data, sw = b"", 0x6700 | min(len(data), 0xff)
            else:
                self.out.append(ins)
        self.finish(data, sw)

    def finish(self, data, sw):
        self.out.extend(data)
        self.out.extend(sw.to_bytes(2, "big"))
        cla, ins, p1, p2, p3 = self.header
        name = (CARD_TO_PHONE.get(ins) or PHONE_TO_CARD.get(ins) or NO_DATA.get(ins)
                or "?") if cla == 0xa0 else "wrong class"
        line = f"{bytes(self.header).hex(' ')}"
        if self.data:
            line += f" | {bytes(self.data).hex(' ')}"
        line += f" -> {bytes(data).hex(' ') + ' ' if data else ''}{sw:04x}  {name}"
        target = self.target_name(ins)
        if target:
            line += f" {target}"
        self.log(line)
        self.header = bytearray()
        self.data = bytearray()
        self.need = 0
        self.last_select = ""

    def target_name(self, ins):
        if ins == 0xa4:
            return self.last_select
        if ins in (0xb0, 0xb2, 0xd6, 0xdc, 0xa2, 0x32) and self.current_ef:
            return self.current_ef.name + (f" rec {self.record}" if ins in (0xb2, 0xdc) else "")
        return ""

    # GSM 11.11 commands

    def find(self, fid):
        """Files SELECT can reach from the current DF (GSM 11.11 6.5): the MF,
        the current DF, its parent, their children; then anywhere."""
        df = self.current_df
        near = [self.files.mf, df] + df.children
        if df.parent:
            near += [df.parent] + df.parent.children
        for f in near:
            if f.fid == fid:
                return f
        for f in self.files.all():
            if f.fid == fid:
                return f
        return None

    def select_response(self, f):
        if f.kind == EF:
            size = len(f.data)
            rest = bytes([0x01, 0x02, f.structure, f.record_len])
            return (bytes(2) + size.to_bytes(2, "big") + f.fid.to_bytes(2, "big")
                    + bytes([EF, 0x00]) + bytes(f.access) + rest)
        dfs = sum(1 for c in f.children if c.kind == DF)
        efs = sum(1 for c in f.children if c.kind == EF)
        # characteristics 0x93: clock stop allowed, 13/4 MHz, 3 V card,
        # CHV1 disabled; 4 secret codes; CHV1/CHV2 initialised with 3
        # tries, unblock codes with 10
        gsm = bytes([0x93, dfs, efs, 0x04, 0x00, 0x83, 0x8a, 0x83, 0x8a])
        return (bytes(2) + (0x1000).to_bytes(2, "big") + f.fid.to_bytes(2, "big")
                + bytes([f.kind]) + bytes(5) + bytes([len(gsm)]) + gsm)

    def command(self, ins, p1, p2, p3, data):
        if ins == 0xa4:                                     # SELECT
            fid = int.from_bytes(data[:2], "big")
            f = self.find(fid)
            self.last_select = f.name if f else f"{fid:04x} (not found)"
            if f is None:
                return b"", 0x9404
            if f.kind == EF:
                self.current_ef = f
                self.record = 0
            else:
                self.current_df, self.current_ef = f, None
            self.response = self.select_response(f)
            return b"", 0x9f00 | len(self.response)
        if ins == 0xc0:                                     # GET RESPONSE
            if not self.response:
                return b"", 0x6f00
            return self.response[:p3 or 256], 0x9000
        if ins == 0xf2:                                     # STATUS
            return self.select_response(self.current_df)[:p3 or 256], 0x9000
        if ins in (0x20, 0x24, 0x26, 0x28, 0x2c, 0x04, 0x44, 0xfa, 0x10, 0x14):
            return b"", 0x9000                              # CHV, (in)validate, profile
        if ins == 0xc2:                                     # ENVELOPE: no toolkit
            return b"", 0x6f00
        if ins == 0x12:                                     # FETCH: nothing pending
            return b"", 0x6f00
        if ins == 0x88:                                     # RUN GSM ALGORITHM
            self.response = bytes.fromhex("01020304") + bytes.fromhex("1112131415161718")
            return b"", 0x9f00 | len(self.response)
        ef = self.current_ef
        if ef is None:
            return b"", 0x9400                              # no EF selected
        if ins == 0xb0 or ins == 0xd6:                      # READ / UPDATE BINARY
            if ef.structure != TRANSPARENT:
                return b"", 0x9408
            off = p1 << 8 | p2
            n = (p3 or 256) if ins == 0xb0 else len(data)
            if off + n > len(ef.data):
                return b"", 0x6b00 if off >= len(ef.data) else 0x6700 | max(0, len(ef.data) - off)
            if ins == 0xb0:
                return bytes(ef.data[off:off + n]), 0x9000
            ef.data[off:off + n] = data
            return b"", 0x9000
        if ins in (0xb2, 0xdc):                             # READ / UPDATE RECORD
            if ef.structure == TRANSPARENT:
                return b"", 0x9408
            n = ef.records()
            if p2 == 0x02:
                rec = self.record + 1 if self.record else 1
            elif p2 == 0x03:
                rec = self.record - 1 if self.record else n
            elif p2 == 0x04:
                rec = p1 or self.record
            else:
                return b"", 0x6b00
            if ef.structure == CYCLIC and ins == 0xdc:
                # cyclic files are updated at the oldest record, which becomes record 1
                ef.data[:] = bytes(data[:ef.record_len]) + ef.data[:-ef.record_len]
                self.record = 1
                return b"", 0x9000
            if not 1 <= rec <= n:
                return b"", 0x9402
            self.record = rec
            start = (rec - 1) * ef.record_len
            if ins == 0xb2:
                return bytes(ef.data[start:start + ef.record_len]), 0x9000
            if len(data) != ef.record_len:
                return b"", 0x6700 | ef.record_len
            ef.data[start:start + ef.record_len] = data
            return b"", 0x9000
        if ins == 0xa2:                                     # SEEK
            if ef.structure == TRANSPARENT:
                return b"", 0x9408
            mode, kind = p2 & 0x0f, p2 >> 4
            n = ef.records()
            if mode == 0x00:
                order = range(1, n + 1)
            elif mode == 0x01:
                order = range(n, 0, -1)
            elif mode == 0x02:
                order = range((self.record or 0) + 1, n + 1)
            else:
                order = range((self.record or n + 1) - 1, 0, -1)
            for rec in order:
                start = (rec - 1) * ef.record_len
                if ef.data[start:start + len(data)] == data:
                    self.record = rec
                    if kind == 1:
                        self.response = bytes([rec])
                        return b"", 0x9f01
                    return b"", 0x9000
            return b"", 0x9404
        if ins == 0x32:                                     # INCREASE
            return b"", 0x9408
        return b"", 0x6d00


class SimController:
    def __init__(self, machine, slots, log_path):
        """slots: set of slot numbers (0-2) with a card in them."""
        self.m = machine
        self.logf = open(log_path, "w", buffering=1)
        files = {s: CardFiles() for s in slots}
        self.cards = {s: Card(files[s], lambda line, s=s: self.log(s, line)) for s in slots}
        self.rx = collections.deque()
        self.status = 0           # latched STS0 bits
        self.pump_due = None      # block when the next byte enters the RX FIFO
        self.tout_armed = False
        self.ctl0 = 0
        self.slot = 0

    def log(self, slot, line):
        self.logf.write(f"{self.m.nblocks / BLOCKS_PER_MS:10.1f} ms slot {slot}: {line}\n")

    def reg(self, off):
        return self.m.regs.get(SIM_BASE + off, 0)

    def card(self):
        return self.cards.get(self.slot)

    # register reads

    def rx_mark(self):
        return self.reg(CTL2) & 0x3f

    def sts0(self):
        value = self.status
        if self.rx and len(self.rx) >= max(1, self.rx_mark()):
            value |= ST_RX_FULL
        return value | ST_TX_EMPTY     # the card takes TX bytes at once

    def read_rx(self):
        value = self.rx.popleft() if self.rx else 0
        self.kick()
        self.arm_timeout()
        self.update_line()
        return value

    def read_sts1(self):
        value = self.reg(STS1) & ~(STS1_CARD_IN | STS1_ACTIVE)
        card = self.card()
        if card:
            value |= STS1_CARD_IN
            if card.active:
                value |= STS1_ACTIVE
        return value

    def read_sts2(self):
        return min(len(self.rx), 0x7f)

    # register writes

    def on_tx(self, value):
        card = self.card()
        if card and self.ctl0 & CTL0_RST:
            card.receive(value & 0xff)
            self.kick(RESPONSE_DELAY_BLOCKS)

    def on_iclr(self, value):
        self.status &= ~value
        self.m.regs[SIM_BASE + ICLR] = 0
        self.update_line()

    def on_ctl0(self, value):
        old, self.ctl0 = self.ctl0, value
        if value & CTL0_RX_FIFO_RST:
            self.rx.clear()
            self.status &= ~ST_RX_TOUT
        card = self.card()
        if card:
            if value & CTL0_RST and not old & CTL0_RST:
                card.cold_reset()
                self.kick(ATR_DELAY_BLOCKS)
            elif old & CTL0_RST and not value & CTL0_RST:
                card.deactivate()
        self.update_line()

    def on_sel(self, value):
        slot = (value >> 2) & 3
        if slot != self.slot:
            self.slot = slot
            self.rx.clear()
            self.update_line()

    def update_line(self):
        self.m.intc.set_line(IRQ_LINE, self.sts0() & self.reg(IE))

    # the card's bytes reaching the RX FIFO

    def kick(self, delay=BYTE_BLOCKS):
        """Schedule the next byte from the card, if it has one."""
        card = self.card()
        if card and card.out and self.pump_due is None:
            self.pump_due = self.m.nblocks + delay
            self.m.schedule(delay, self.pump)

    def pump(self):
        self.pump_due = None
        card = self.card()
        if card and card.out and len(self.rx) < FIFO_SIZE:
            self.rx.append(card.out.popleft())
        self.kick()
        self.arm_timeout()
        self.update_line()

    def arm_timeout(self):
        card = self.card()
        if self.rx and not (card and card.out) and not self.tout_armed:
            self.tout_armed = True
            self.m.schedule(RX_TIMEOUT_BLOCKS, self.rx_timeout)

    def rx_timeout(self):
        self.tout_armed = False
        card = self.card()
        if card and card.out:
            return
        if self.rx and len(self.rx) < max(1, self.rx_mark()):
            self.status |= ST_RX_TOUT
            self.update_line()


def install(machine, read_handlers, write_handlers):
    sim = machine.sim
    read_handlers[SIM_BASE + RX] = lambda m: sim.read_rx()
    read_handlers[SIM_BASE + STS0] = lambda m: sim.sts0()
    read_handlers[SIM_BASE + STS1] = lambda m: sim.read_sts1()
    read_handlers[SIM_BASE + STS2] = lambda m: sim.read_sts2()
    write_handlers[SIM_BASE + TX] = lambda m, v: sim.on_tx(v)
    write_handlers[SIM_BASE + ICLR] = lambda m, v: sim.on_iclr(v)
    write_handlers[SIM_BASE + IE] = lambda m, v: sim.update_line()
    write_handlers[SIM_BASE + CTL0] = lambda m, v: sim.on_ctl0(v)
    write_handlers[SIM_BASE + CTL2] = lambda m, v: sim.update_line()
    write_handlers[PIN_CTL0] = lambda m, v: sim.on_sel(v)
