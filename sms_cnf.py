"""MO SMS send confirmation (--sms-send-ok, --log-sms-cnf).

The emulator has no network: a submitted MO text (MNSMS_AppSendSmsEx) goes to
P_MN, which answers the APP task with APP_MN_SEND_SMS_CNF 0xa1a3 and body
cause 2 (send failure; nokia105 lugh/shell/sms_send.c header: body+0x00 =
cause, 0 sent, 2 send failure, 4 MN busy, 9 not ready).  The handset on a
real network gets cause 0.

--sms-send-ok plays the network accepting the text: at the entry of the
flash SCI OS SCI_SendSignal (rtos.a threadx_os.o, flash 0x1936c; r0 = the
signal header, r1 = the receiver) a 0xa1a3 signal has its cause (header
+0x10, the u32 the port's wait_cnf and the stock app read) set to 0 before
it is queued.  Everything else of the CNF is MN's own.  Every 0xa1a3 seen
there (with or without the option) is logged to OUT/sms-cnf.log: time,
receiver, cause before and after.
"""
import struct

import unicorn.arm_const as arm
from unicorn import UC_HOOK_CODE

APP_MN_SEND_SMS_CNF = 0xa1a3
SEND_SIGNAL = (0x0001936c, 0x1001936c)   # flash SCI_SendSignal (run from the remapped flash, or its mapping)
CAUSE_OFF = 0x10


class SmsCnf:
    def __init__(self, machine, ok, blocks_per_ms):
        self.m = machine
        self.ok = ok
        self.bpm = blocks_per_ms
        self.n = 0
        self.log = open(machine.args.out / "sms-cnf.log", "w", buffering=1)
        self.w(f"armed: --sms-send-ok {'on (cause -> 0)' if ok else 'off (log only)'}")
        for a in SEND_SIGNAL:
            machine.uc.hook_add(UC_HOOK_CODE, machine._guard(self.on_send), begin=a, end=a)

    def w(self, line):
        self.log.write(f"{self.m.nblocks / self.bpm:10.1f} ms {line}\n")

    def on_send(self, uc, addr, size, _):
        sig = uc.reg_read(arm.UC_ARM_REG_R0)
        rcv = uc.reg_read(arm.UC_ARM_REG_R1)
        try:
            code = struct.unpack("<H", bytes(uc.mem_read(sig, 2)))[0]
        except Exception:  # noqa: BLE001  (not a readable header: not ours)
            return
        if code != APP_MN_SEND_SMS_CNF:
            return
        cause = struct.unpack("<I", bytes(uc.mem_read(sig + CAUSE_OFF, 4)))[0]
        self.n += 1
        new = cause
        if self.ok and cause != 0:
            uc.mem_write(sig + CAUSE_OFF, struct.pack("<I", 0))
            new = 0
        self.w(f"CNF {self.n}: SCI_SendSignal(0x{sig:08x}, receiver {rcv}) at 0x{addr:08x}: 0xa1a3 cause {cause} -> {new}")


def install(machine, ok, blocks_per_ms):
    return SmsCnf(machine, ok, blocks_per_ms)
