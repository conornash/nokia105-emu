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

--sms-cnf-delay MS plays a real network's round trip: the CNF to the APP
task (receiver 21, P_APP) is not queued when MN sends it (SCI_SendSignal
returns 0 at once, the signal is kept); MS ms later, at the APP task's next
SCI_PeekSignal entry (the user veneer 0x0810e4ac that nokia105 sms_send.c
wait_cnf spins on, or the flash one 0x19408), registers are saved and
SCI_SendSignal(signal, 21) is called with lr = that entry; on the return
there the registers are restored and the peek runs, so the CNF is in the
queue behind whatever arrived during the wait (keys, timers), as on a real
network.  Without --sms-cnf-delay the CNF is queued when MN sends it.
"""
import struct

import unicorn.arm_const as arm
from unicorn import UC_HOOK_CODE

APP_MN_SEND_SMS_CNF = 0xa1a3
SEND_SIGNAL = (0x0001936c, 0x1001936c)   # flash SCI_SendSignal (run from the remapped flash, or its mapping)
CAUSE_OFF = 0x10
P_APP = 21
PEEK = (0x0810e4ac, 0x00019408, 0x10019408)   # user veneer SCI_PeekSignal, flash SCI_PeekSignal
REGS = [getattr(arm, f"UC_ARM_REG_R{i}") for i in range(13)] + [arm.UC_ARM_REG_SP, arm.UC_ARM_REG_LR,
                                                                arm.UC_ARM_REG_CPSR]


class SmsCnf:
    def __init__(self, machine, ok, blocks_per_ms, delay_ms=0):
        self.m = machine
        self.ok = ok
        self.delay = delay_ms
        self.held = []          # (due_ms, sig, send_addr, thumb)
        self.active = None      # (saved regs, sp, sig) while our SCI_SendSignal runs
        self.resend = None
        self.requeues = 0
        self.bpm = blocks_per_ms
        self.n = 0
        self.log = open(machine.args.out / "sms-cnf.log", "w", buffering=1)
        self.w(f"armed: --sms-send-ok {'on (cause -> 0)' if ok else 'off (log only)'}; --sms-cnf-delay {delay_ms} ms")
        for a in SEND_SIGNAL:
            machine.uc.hook_add(UC_HOOK_CODE, machine._guard(self.on_send), begin=a, end=a)
        if delay_ms:
            for a in PEEK:
                machine.uc.hook_add(UC_HOOK_CODE, machine._guard(self.on_peek), begin=a, end=a)

    def now(self):
        return self.m.nblocks / self.bpm

    def on_peek(self, uc, addr, size, _):
        sp = uc.reg_read(arm.UC_ARM_REG_SP)
        if self.active is not None:
            saved, mark, sig = self.active
            if sp != mark:
                return
            r0 = uc.reg_read(arm.UC_ARM_REG_R0)
            self.active = None
            for r, v in zip(REGS, saved):
                uc.reg_write(r, v)
            self.w(f"held CNF 0x{sig:08x} queued to P_APP (SCI_SendSignal returned {r0}); the peek at 0x{addr:08x} proceeds")
            return
        if not self.held or self.now() < self.held[0][0] or uc.reg_read(arm.UC_ARM_REG_R0) != P_APP:
            return
        due, sig, send_addr, thumb = self.held.pop(0)
        saved = [uc.reg_read(r) for r in REGS]
        t_here = (uc.reg_read(arm.UC_ARM_REG_CPSR) >> 5) & 1
        self.active = (saved, sp, sig)
        self.resend = sig
        uc.reg_write(arm.UC_ARM_REG_R0, sig)
        uc.reg_write(arm.UC_ARM_REG_R1, P_APP)
        uc.reg_write(arm.UC_ARM_REG_LR, addr | t_here)
        uc.reg_write(arm.UC_ARM_REG_PC, send_addr | thumb)
        self.w(f"held CNF 0x{sig:08x} due {due:.1f} ms: SCI_SendSignal(0x{sig:08x}, 21) from the peek at 0x{addr:08x}")

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
            lr = uc.reg_read(arm.UC_ARM_REG_LR)
            if self.requeues < 400 and rcv == P_APP and 0x04100000 <= lr < 0x04300000:
                self.requeues += 1      # the port requeues a signal it took off the APP queue's head
                self.w(f"requeue {self.requeues}: SCI_SendSignal(0x{sig:08x} code 0x{code:04x}, 21) lr 0x{lr:08x}")
            return
        cause = struct.unpack("<I", bytes(uc.mem_read(sig + CAUSE_OFF, 4)))[0]
        if self.resend == sig:
            self.resend = None
            return
        self.n += 1
        new = cause
        if self.ok and cause != 0:
            uc.mem_write(sig + CAUSE_OFF, struct.pack("<I", 0))
            new = 0
        self.w(f"CNF {self.n}: SCI_SendSignal(0x{sig:08x}, receiver {rcv}) at 0x{addr:08x} lr 0x{uc.reg_read(arm.UC_ARM_REG_LR):08x}: "
               f"0xa1a3 cause {cause} -> {new}")
        lr = uc.reg_read(arm.UC_ARM_REG_LR)
        if self.delay and rcv == P_APP and lr >= 0x08000000:     # MN's own send (user code), not a requeue
            thumb = (uc.reg_read(arm.UC_ARM_REG_CPSR) >> 5) & 1
            self.held.append((self.now() + self.delay, sig, addr, thumb))
            uc.reg_write(arm.UC_ARM_REG_R0, 0)
            uc.reg_write(arm.UC_ARM_REG_PC, lr)
            self.w(f"CNF {self.n} held {self.delay} ms (SCI_SendSignal skipped, returns 0 to lr 0x{lr:08x})")


def install(machine, ok, blocks_per_ms, delay_ms=0):
    return SmsCnf(machine, ok, blocks_per_ms, delay_ms)
