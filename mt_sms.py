"""Live mobile-terminated SMS (--inject-sms MS:NUMBER:TEXT, repeatable).

A text that arrives while the phone is on takes a path no SIM record present
at power-on ever takes: the MN layer (P_MN) stores the SMS-DELIVER, then
sends APP_MN_SMS_IND (0xa1a7 on this handset; 2013 reference 0xa1a4) to the
APP task, whose dispatch hands it to the SMS app's ProcessMsg (RAM word
0x04358444) -> MMISMS_HandleSMSMTInd (user 0x08210d9c) -> OperateMtIndAsSms
-> InsertNewMtToOrder (the unread count to_be_read_msg_num, 0x04358236).

The emulator has no network and no MN-level SMS-DELIVER, so this module
plays the MN layer's two steps at emulated time MS:

  (i)  the store: the first free EF_SMS record of the inserted card
       (sim.py, status byte 00) becomes status 03 (received, unread) +
       SMSC + SMS-DELIVER from NUMBER, PID 00, DCS 08 (UCS-2), SCTS = the
       emulated clock (--rtc-time + MS), UD = TEXT in UTF-16BE.  record_id
       = that record's index (0-based, as the stock layer numbers SIM
       records), storage 2 (SIM).  The firmware sees the record when it
       next reads EF_SMS (READ RECORD), as it would after MN's own UPDATE
       RECORD.  MN's RAM-side bookkeeping of SIM records is NOT updated:
       the emulator does not know where P_MN keeps it.
  (ii) the signal: at the next entry of the APP task's per-signal dispatch
       (kern 0x040205d8: r0 = &signal; timer, key and system signals all
       pass here, at the top of the task's loop) the registers are saved
       and DispatchSysSig (kern 0x04020594) is called with a 16-byte
       signal header (code 0xa1a7, size, sender 0) + an MnSmsMtIndS body
       built on the APP task's stack:
         +0x00 cause 0, +0x04 record_id (u16), +0x08 storage 2,
         +0x0c MN_SMS_MT_SMS_T (2013 mn_type.h): +0x10 sc_addr_t,
         +0x30 origin_addr_t (type, plan, num_len u16 @+0x38, BCD @+0x3a),
         +0x50 pid, +0x54 dcs (0x20 bytes here; alphabet @+0x68 = 2
         UCS-2: the decoder reads [dcs+0x14], user 0x08072316), +0x74 time_stamp_t (7 x u8, binary, year - 2000), +0x7b
         user_data_t (+0x7c length u8, +0x7d data: the decoder
         0x08072276 reads [ud+1] as the length and copies from ud+2), +0x10c dual_sys 0.  This
         build differs from the 2013 header by 4 bytes in the dcs: the
         handler passes body+0x54 (dcs) and body+0x7b (user data) to
         MMISMS_DecodeUserHeadAndContent (call at user 0x08210e60) and
         reads dual_sys at body+0x100+0xc (0x08210df4..), which
         MNSMS_DecodeUserDataEx asserts is < 2 (0x08071104).
       DispatchSysSig walks g_reg_app as for a real indication, so the
       stock handlers (and whatever wraps the SMS app's ProcessMsg) run on
       the real stack.  On return to 0x040205d8 every register is
       restored and the signal the task was about to dispatch proceeds.

Addresses are this handset's (Nokia 105 TA-1557 dump): kern 0x040205d8 /
0x04020594 are fixed kern code.  Every step is logged to OUT/inject-sms.log.
"""
import datetime
import struct

import app_dispatch
from app_dispatch import APP_DISPATCH, DISPATCH_SYS_SIG, REGS  # noqa: F401  (re-exported)

APP_MN_SMS_IND = 0xa1a7
EF_SMS = 0x6f3c
STORAGE_SIM = 2
BODY_LEN = 0x110
SMSC = "+447000000000"
UNREAD = 0x04358236             # s_mmisms_order +0x36 to_be_read_msg_num (u16), logged before/after



def parse(spec):
    """MS:NUMBER:TEXT -> (ms, number, text); TEXT may contain ':'"""
    parts = spec.split(":", 2)
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].lstrip("+").isdigit() or not parts[2]:
        raise ValueError(f"--inject-sms {spec!r}: want MS:NUMBER:TEXT, e.g. 25000:+447700900123:Hello")
    return int(parts[0]), parts[1], parts[2]


def bcd(digits):
    digits = digits + "f" * (len(digits) % 2)
    return bytes(int(digits[i + 1] + digits[i], 16) for i in range(0, len(digits), 2))


def semi(v):
    return ((v % 10) << 4) | (v // 10)


def tpdu(number, ucs2, ts):
    """SMSC + SMS-DELIVER as GSM 11.11 stores it after the status byte"""
    sc = bytes([0x91]) + bcd(SMSC.lstrip("+"))
    digits = number.lstrip("+")
    oa = bytes([len(digits), 0x91 if number.startswith("+") else 0x81]) + bcd(digits)
    scts = bytes(semi(v) for v in (ts.year % 100, ts.month, ts.day, ts.hour, ts.minute, ts.second, 0))
    return bytes([len(sc)]) + sc + bytes([0x04]) + oa + bytes([0x00, 0x08]) + scts + bytes([len(ucs2)]) + ucs2


def called_number(b, off, number):
    digits = bcd(number.lstrip("+"))
    struct.pack_into("<IIH", b, off, 1 if number.startswith("+") else 0, 1, len(digits))
    b[off + 10:off + 10 + len(digits)] = digits


def body(rid, number, ucs2, ts):
    b = bytearray(BODY_LEN)
    struct.pack_into("<IHxxI", b, 0x00, 0, rid, STORAGE_SIM)
    called_number(b, 0x10, SMSC)
    called_number(b, 0x30, number)
    struct.pack_into("<I", b, 0x58, 1)              # sms_class 1 (class_is_present 0)
    struct.pack_into("<I", b, 0x68, 2)              # alphabet UCS-2
    b[0x74:0x7b] = bytes((ts.year - 2000, ts.month, ts.day, ts.hour, ts.minute, ts.second, 0))
    b[0x7c] = len(ucs2)                             # MNSMS_DecodeUserDataEx reads the length at
    b[0x7d:0x7d + len(ucs2)] = ucs2                 # user_data+1, the data from +2 (user 0x080722be)
    struct.pack_into("<I", b, 0x10c, 0)            # dual_sys SIM1
    return bytes(b)


class Injector:
    def __init__(self, machine, specs, blocks_per_ms):
        self.m = machine
        self.bpm = blocks_per_ms
        self.pending = sorted(specs)
        self.log = open(machine.args.out / "inject-sms.log", "w", buffering=1)
        card = machine.sim.cards[machine.args.sim_slot]
        self.ef = next(f for f in card.files.all() if f.fid == EF_SMS)
        q = app_dispatch.queue(machine, blocks_per_ms)
        for ms, number, text in self.pending:
            q.add(ms, self.fire(ms, number, text), self.done)
            self.w(f"armed {ms} ms from {number}: {text!r}")

    def w(self, line):
        self.log.write(f"{self.m.nblocks / self.bpm:10.1f} ms {line}\n")

    def unread(self):
        return struct.unpack("<H", bytes(self.m.uc.mem_read(UNREAD, 2)))[0]

    def store(self, number, ucs2, ts):
        n = self.ef.record_len
        for i in range(self.ef.records()):
            if self.ef.data[i * n] & 1 == 0:          # status 00 = free
                rec = (b"\x03" + tpdu(number, ucs2, ts)).ljust(n, b"\xff")
                self.ef.data[i * n:(i + 1) * n] = rec
                return i
        return None

    def fire(self, ms, number, text):
        def go(uc):
            ts = self.m.args.rtc_time + datetime.timedelta(milliseconds=ms)
            ucs2 = text.encode("utf-16-be")[:140]
            rid = self.store(number, ucs2, ts)
            if rid is None:
                self.w(f"EF_SMS full: {text!r} from {number} not stored, no signal (MN would send MEM_FULL)")
                return None
            self.w(f"EF_SMS record {rid + 1} (record_id {rid}) = status 03, from {number}, "
                   f"SCTS {ts:%Y-%m-%d %H:%M:%S}, {text!r}")

            def note(base, saved):
                self.w(f"signal 0x{APP_MN_SMS_IND:04x} at 0x{base:08x} (body 0x{base + 0x10:08x}: cause 0, "
                       f"record_id {rid}, storage {STORAGE_SIM}) -> DispatchSysSig from the APP dispatch, "
                       f"interrupted lr 0x{saved[14]:08x}; unread before {self.unread()}")
            return APP_MN_SMS_IND, body(rid, number, ucs2, ts), note
        return go

    def done(self, r0):
        self.w(f"DispatchSysSig returned r0=0x{r0:x}; unread after {self.unread()}; the task's own signal proceeds")


def install(machine, specs, blocks_per_ms):
    if machine.sim is None:
        raise SystemExit("--inject-sms needs --sim (the text is stored on the SIM)")
    return Injector(machine, specs, blocks_per_ms)
