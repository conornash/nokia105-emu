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

Not modelled: MN's own state (the call id is not registered in P_MN, so an
answer or reject the firmware sends back to MN has nothing to act on), the
ring tone's audio path beyond whatever the stock handler starts, and a
second injection falling on the same APP dispatch as an --inject-sms.

Every step is logged to OUT/inject-call.log.
"""
import struct

import unicorn.arm_const as arm
from unicorn import UC_HOOK_CODE

from mt_sms import APP_DISPATCH, DISPATCH_SYS_SIG, REGS, bcd

APP_MN_SETUP_IND = 0xa15d
APP_MN_DISCONNECTED_IND = 0xa15b
SETUP_LEN = 0x70
DISC_LEN = 0x0c
CALL_ID = 0
CAUSE_NORMAL_CLEARING = 16


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


def disc_body():
    b = bytearray(DISC_LEN)
    b[0] = CALL_ID
    struct.pack_into("<II", b, 4, CAUSE_NORMAL_CLEARING, 0)
    return bytes(b)


class Injector:
    def __init__(self, machine, specs, blocks_per_ms):
        self.m = machine
        self.bpm = blocks_per_ms
        self.pending = []           # (ms, sig, body, what)
        for ms, number, end in specs:
            self.pending.append((ms, APP_MN_SETUP_IND, setup_body(number), f"SETUP_IND from {number}"))
            if end is not None:
                self.pending.append((end, APP_MN_DISCONNECTED_IND, disc_body(), f"DISCONNECTED_IND of {number}"))
        self.pending.sort(key=lambda p: p[0])
        self.active = None
        self.log = open(machine.args.out / "inject-call.log", "w", buffering=1)
        machine.uc.hook_add(UC_HOOK_CODE, machine._guard(self.on_dispatch),
                            begin=APP_DISPATCH, end=APP_DISPATCH)
        for ms, sig, _, what in self.pending:
            self.w(f"armed {ms} ms: 0x{sig:04x} {what}")

    def w(self, line):
        self.log.write(f"{self.m.nblocks / self.bpm:10.1f} ms {line}\n")

    def on_dispatch(self, uc, addr, size, _):
        sp = uc.reg_read(arm.UC_ARM_REG_SP)
        if self.active is not None:
            saved, mark = self.active
            if sp == mark:
                for r, v in zip(REGS, saved):
                    uc.reg_write(r, v)
                self.active = None
                self.w(f"DispatchSysSig returned r0=0x{uc.reg_read(arm.UC_ARM_REG_R0):x}; "
                       f"the task's own signal proceeds")
            return
        if not self.pending or self.m.nblocks < self.pending[0][0] * self.bpm:
            return
        ms, sig, body, what = self.pending.pop(0)
        saved = [uc.reg_read(r) for r in REGS]
        base = (sp - 0x400) & ~7
        uc.mem_write(base, struct.pack("<HHIII", sig, 0x10 + len(body), 0, 0, 0) + body)
        mark = base - 8
        uc.reg_write(arm.UC_ARM_REG_R0, base)
        uc.reg_write(arm.UC_ARM_REG_SP, mark)
        uc.reg_write(arm.UC_ARM_REG_LR, APP_DISPATCH | 1)
        uc.reg_write(arm.UC_ARM_REG_PC, DISPATCH_SYS_SIG | 1)
        self.active = (saved, mark)
        self.w(f"{what} (armed {ms} ms): signal 0x{sig:04x} at 0x{base:08x}, body 0x{base + 0x10:08x} "
               f"{body.hex()} -> DispatchSysSig from the APP dispatch, interrupted lr 0x{saved[14]:08x}")


def install(machine, specs, blocks_per_ms):
    return Injector(machine, specs, blocks_per_ms)
