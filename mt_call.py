"""Live mobile-terminated call (--inject-call MS:NUMBER[:ENDMS], repeatable).

An incoming call reaches the handset as the MN layer's (P_MN) CC indication
APP_MN_SETUP_IND, signal 0xa15d on this handset (2013 reference 0xa15a), sent
to the APP task; its dispatch hands it to the CC app's ProcessMsg, which is
where the stock incoming-call window starts and where LughOS's tel_bridge.c
(wrap_cc, cc_decode) reads the call_id, dual_sys and the caller digits.  When
the far end hangs up, MN sends APP_MN_DISCONNECTED_IND 0xa15b the same way.

The emulator has no network, so this module plays the MN layer at emulated
time MS (and, if ENDMS is given, ENDMS):

  at the next entry of the APP task's per-signal dispatch (kern 0x040205d8:
  r0 = &signal; timer, key and system signals all pass here, at the top of
  the task's loop) the registers are saved and DispatchSysSig (kern
  0x04020594) is called with a 16-byte signal header (code, size, sender 0)
  + the body, built on the APP task's stack.  DispatchSysSig walks g_reg_app
  as for a real indication, so the stock handlers (and whatever wraps the CC
  app's ProcessMsg) run on the real stack.  On return to 0x040205d8 every
  register is restored and the signal the task was about to dispatch
  proceeds.  The mechanism is mt_sms.py's.

Bodies (offsets as nokia105/lugh/fw.h and lugh/shell/tel_bridge.h document
them, confirmed against stock CC ProcessMsg 0x081a10e9 reading 0xa15d):

  SETUP_IND 0xa15d, 0x70 bytes:
    +0x00 u8 call_id (0), +0x01 u8 calling_num_present 1,
    +0x04 MN_CALLING_NUMBER_T: +0x04 u32 number_type (1 = international,
          from a leading '+'), +0x08 plan 1 (ISDN), +0x0c present_ind 0
          (allowed), +0x10 screen_ind 0, +0x14 u16 num_len (BCD bytes),
          +0x16 BCD digits, low nibble first, 0xf filler,
    +0x64 u32 dual_sys 0 (SIM1), +0x68 u32 call_type 0 (normal voice; the
          stock handler refuses 2 = data), +0x6c bearer 0.
  DISCONNECTED_IND 0xa15b, 0x0c bytes: +0 u8 call_id, +4 u32 cause 16
    (MN_CAUSE_NORMAL_CLEARING), +8 u32 dual_sys 0.

Further CC events (--inject-call-event MS:KIND[:ARG], repeatable), the same
way, call_id 0, SIM1.  Ids: this image's CC range is the 2013 list shifted by
+1 (range start APP_MN_CALL_SIGNAL_START 0xa155 here, 0xa154 in 2013
signal_id_header_gen.h; SETUP_IND 0xa15d / 0xa15c, CALL_DISCONNECTED_IND
0xa15b / 0xa15a, CALL_ERR_IND 0xa15c / 0xa15b, all three confirmed by the
literals in our CC app).  CC ProcessMsg 0x081a10e9 dispatches on sig - 0xa156
(literal 0xffff5eaa at 0x081a14f4) through a switch8 table at 0x081a115a whose
entries 2, 3, 4 (0xa158, 0xa159, 0xa15a) are distinct cases:
  alerting[:TYPE]  APP_MN_ALERTING_IND 0xa158 (2013 0xa157), 0x0c bytes:
                   +0 u8 call_id, +4 u32 alerting_type (TYPE, default 0),
                   +8 u32 dual_sys.  (the far end ringing, MO call)
  connect          APP_MN_SETUP_COMPLETE_IND 0xa159 (2013 0xa158), 8 bytes:
                   +0 u8 call_id, +4 u32 dual_sys.  (MT: our answer done)
  connect:mo       APP_MN_SETUP_COMPLETE_CNF 0xa15a (2013 0xa159), 0x68
                   bytes: +0 call_id, +1 connected_num_present 0, +0x64
                   dual_sys.  (MO: the far end answered; 2013 mmicc_app.c
                   CC_SetupCompleteCnf is the "MO" handler)
  disc:CAUSE       APP_MN_CALL_DISCONNECTED_IND 0xa15b, cause CAUSE
  err:CAUSE        APP_MN_CALL_ERR_IND 0xa15c, +0 call_id, +4 error_cause,
                   +8 dual_sys
All injections (calls, events, --inject-sms) share one ordered queue on the
APP dispatch (app_dispatch.py), so any number may fall on the same ms.

Calls the firmware makes into MN (any caller: the LughOS shell or stock
code; lr tells which) are logged at the user entries of MNCALL_StartCallEx
0x0823f04d, MNCALL_ConnectCallEx 0x0823f241 and MNCALL_DisconnectCallEx
0x0823f307 (lugh/fw.h), as "call NAME(args) lr=..." lines.

Not modelled: MN's own state (the call id is not registered in P_MN, so an
answer or reject the firmware sends back to MN has nothing to act on; MN
never answers by itself: use --inject-call-event; on the LughOS image a
`connect` after the shell's answer is followed within ~1 s by a restart into
the boot ROM, mt_call_selftest.py run "connect"), and the ring tone's
audio path beyond whatever the stock handler starts.

Every step is logged to OUT/inject-call.log.
"""
import struct

import unicorn.arm_const as arm
from unicorn import UC_HOOK_CODE

import app_dispatch
from mt_sms import bcd

APP_MN_SETUP_IND = 0xa15d
APP_MN_DISCONNECTED_IND = 0xa15b
SETUP_LEN = 0x70
DISC_LEN = 0x0c
CALL_ID = 0
CAUSE_NORMAL_CLEARING = 16
APP_MN_ALERTING_IND = 0xa158
APP_MN_SETUP_COMPLETE_IND = 0xa159
APP_MN_SETUP_COMPLETE_CNF = 0xa15a
APP_MN_CALL_ERR_IND = 0xa15c
EVENT_KINDS = ("alerting", "connect", "disc", "err")

MNCALL = {0x0823f04c: "StartCallEx", 0x0823f240: "ConnectCallEx", 0x0823f306: "DisconnectCallEx"}


def parse(spec):
    """MS:NUMBER[:ENDMS] -> (ms, number, endms or None)"""
    parts = spec.split(":")
    ok = (2 <= len(parts) <= 3 and parts[0].isdigit() and parts[1].lstrip("+").isdigit()
          and (len(parts) == 2 or parts[2].isdigit()))
    if not ok:
        raise ValueError(f"--inject-call {spec!r}: want MS:NUMBER[:ENDMS], e.g. 25000:07700900461:40000")
    ms, end = int(parts[0]), int(parts[2]) if len(parts) == 3 else None
    if end is not None and end <= ms:
        raise ValueError(f"--inject-call {spec!r}: ENDMS must be after MS")
    return ms, parts[1], end


def parse_event(spec):
    """MS:KIND[:ARG] -> (ms, kind, arg)"""
    parts = spec.split(":")
    bad = ValueError(f"--inject-call-event {spec!r}: want MS:KIND[:ARG], KIND one of alerting[:TYPE], "
                     f"connect[:mo], disc:CAUSE, err:CAUSE, e.g. 30000:disc:16")
    if not (2 <= len(parts) <= 3 and parts[0].isdigit() and parts[1] in EVENT_KINDS):
        raise bad
    ms, kind, arg = int(parts[0]), parts[1], parts[2] if len(parts) == 3 else None
    if kind in ("disc", "err") and (arg is None or not arg.isdigit()):
        raise bad
    if kind == "alerting" and arg is not None and not arg.isdigit():
        raise bad
    if kind == "connect" and arg not in (None, "mo"):
        raise bad
    return ms, kind, arg


def event_signal(kind, arg):
    """-> (sig, body, what)"""
    if kind == "alerting":
        t = int(arg or 0)
        return APP_MN_ALERTING_IND, struct.pack("<BxxxII", CALL_ID, t, 0), f"ALERTING_IND type {t}"
    if kind == "connect" and arg == "mo":
        b = bytearray(SETUP_LEN - 8)
        b[0] = CALL_ID
        return APP_MN_SETUP_COMPLETE_CNF, bytes(b), "SETUP_COMPLETE_CNF (MO connected)"
    if kind == "connect":
        return APP_MN_SETUP_COMPLETE_IND, struct.pack("<BxxxI", CALL_ID, 0), "SETUP_COMPLETE_IND (MT connected)"
    sig = APP_MN_DISCONNECTED_IND if kind == "disc" else APP_MN_CALL_ERR_IND
    name = "DISCONNECTED_IND" if kind == "disc" else "CALL_ERR_IND"
    return sig, struct.pack("<BxxxII", CALL_ID, int(arg), 0), f"{name} cause {int(arg)}"


def setup_body(number):
    b = bytearray(SETUP_LEN)
    digits = bcd(number.lstrip("+"))
    b[0x00] = CALL_ID
    b[0x01] = 1
    struct.pack_into("<IIIIH", b, 0x04, 1 if number.startswith("+") else 0, 1, 0, 0, len(digits))
    b[0x16:0x16 + len(digits)] = digits
    struct.pack_into("<I", b, 0x64, 0)      # dual_sys SIM1
    struct.pack_into("<I", b, 0x68, 0)      # call_type normal
    return bytes(b)


def disc_body(cause=CAUSE_NORMAL_CLEARING):
    b = bytearray(DISC_LEN)
    b[0] = CALL_ID
    struct.pack_into("<II", b, 4, cause, 0)
    return bytes(b)


class Injector:
    def __init__(self, machine, specs, events, blocks_per_ms):
        self.m = machine
        self.bpm = blocks_per_ms
        self.log = open(machine.args.out / "inject-call.log", "w", buffering=1)
        q = app_dispatch.queue(machine, blocks_per_ms)
        items = []
        for ms, number, end in specs:
            items.append((ms, APP_MN_SETUP_IND, setup_body(number), f"SETUP_IND from {number}"))
            if end is not None:
                items.append((end, APP_MN_DISCONNECTED_IND, disc_body(), f"DISCONNECTED_IND of {number}"))
        for ms, kind, arg in events:
            items.append((ms, *event_signal(kind, arg)))
        items.sort(key=lambda p: p[0])
        for ms, sig, body, what in items:
            q.add(ms, self.fire(ms, sig, body, what), self.done)
            self.w(f"armed {ms} ms: 0x{sig:04x} {what}")
        for addr in MNCALL:
            machine.uc.hook_add(UC_HOOK_CODE, machine._guard(self.on_mncall), begin=addr, end=addr)

    def w(self, line):
        self.log.write(f"{self.m.nblocks / self.bpm:10.1f} ms {line}\n")

    def fire(self, ms, sig, body, what):
        def go(uc):
            def note(base, saved):
                self.w(f"{what} (armed {ms} ms): signal 0x{sig:04x} at 0x{base:08x}, body 0x{base + 0x10:08x} "
                       f"{body.hex()} -> DispatchSysSig from the APP dispatch, interrupted lr 0x{saved[14]:08x}")
            return sig, body, note
        return go

    def done(self, r0):
        self.w(f"DispatchSysSig returned r0=0x{r0:x}; the task's own signal proceeds")

    def on_mncall(self, uc, addr, size, _):
        r = [uc.reg_read(getattr(arm, f"UC_ARM_REG_R{i}")) for i in range(4)]
        lr = uc.reg_read(arm.UC_ARM_REG_LR)
        name = MNCALL[addr]
        if name == "StartCallEx":
            sp = uc.reg_read(arm.UC_ARM_REG_SP)
            clir, utu = struct.unpack("<II", bytes(uc.mem_read(sp, 8)))
            num = ""
            try:
                raw = bytes(uc.mem_read(r[2], 0x1e))
                n = min(struct.unpack_from("<H", raw, 8)[0], 20)
                for byte in raw[0xa:0xa + n]:
                    for d in (byte & 0xf, byte >> 4):
                        if d != 0xf:
                            num += "0123456789*#abc"[d] if d < 15 else ""
                num = ("+" if raw[0] == 1 else "") + num
            except Exception:  # noqa: BLE001  (an unreadable pointer is logged as such)
                num = "?"
            args = (f"dual_sys={r[0]} call_type={r[1]} called=0x{r[2]:08x} ({num!r}) subaddr=0x{r[3]:08x} "
                    f"clir={clir} utu=0x{utu:08x}")
        elif name == "ConnectCallEx":
            args = f"dual_sys={r[0]} call_id={r[1] & 0xff} utu=0x{r[2]:08x}"
        else:
            args = f"dual_sys={r[0]} call_id={r[1] & 0xff} cause={r[2]} utu=0x{r[3]:08x}"
        self.w(f"call {name}({args}) lr=0x{lr:08x}")
        fail = getattr(self.m.args, "fail_startcall", None)
        if name == "StartCallEx" and fail is not None:
            uc.reg_write(arm.UC_ARM_REG_R0, fail & 0xffffffff)
            uc.reg_write(arm.UC_ARM_REG_PC, lr)          # return at once; lr keeps the caller's Thumb bit
            self.w(f"--fail-startcall: StartCallEx returns {fail} without running (lr=0x{lr:08x})")


def install(machine, specs, blocks_per_ms, events=()):
    return Injector(machine, specs, events, blocks_per_ms)
