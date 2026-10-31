"""One queue of injected signals on the APP task's dispatch (shared by
mt_sms.py and mt_call.py).

Each injector used to hook APP_DISPATCH with its own single slot, so an
--inject-sms and an --inject-call due on the same dispatch both redirected
the same instruction and the second saved the first's half-built frame as
"the task's registers".  Now there is one hook and one ordered queue: at an
entry of the APP task's per-signal dispatch (kern 0x040205d8, r0 = &signal)
the earliest due item fires: registers saved, a 16-byte signal header (code,
size, sender 0) + body built 0x400 below sp, DispatchSysSig (kern 0x04020594)
called with lr = the dispatch.  When it returns there (sp == the mark) every
register is restored and, if another item is already due, it fires at once
from the same entry, before the task's own signal proceeds.  Items due at the
same ms fire in the order they were added.
"""
import struct

import unicorn.arm_const as arm
from unicorn import UC_HOOK_CODE

APP_DISPATCH = 0x040205d8       # kern: the APP task's dispatch of one received signal
DISPATCH_SYS_SIG = 0x04020594   # kern: walk g_reg_app, call app->ProcessMsg

REGS = [getattr(arm, f"UC_ARM_REG_R{i}") for i in range(13)] + [arm.UC_ARM_REG_SP, arm.UC_ARM_REG_LR,
                                                                arm.UC_ARM_REG_CPSR]


class AppQueue:
    def __init__(self, machine, blocks_per_ms):
        self.m = machine
        self.bpm = blocks_per_ms
        self.pending = []       # (ms, seq, fire, done)
        self.seq = 0
        self.active = None      # (saved, mark, done)
        machine.uc.hook_add(UC_HOOK_CODE, machine._guard(self.on_dispatch),
                            begin=APP_DISPATCH, end=APP_DISPATCH)

    def add(self, ms, fire, done):
        """fire(uc) -> None (skip) or (sig, body, note(base, saved)); done(r0) after DispatchSysSig returns."""
        self.pending.append((ms, self.seq, fire, done))
        self.seq += 1
        self.pending.sort(key=lambda p: (p[0], p[1]))

    def on_dispatch(self, uc, addr, size, _):
        sp = uc.reg_read(arm.UC_ARM_REG_SP)
        if self.active is not None:
            saved, mark, done = self.active
            if sp != mark:
                return
            r0 = uc.reg_read(arm.UC_ARM_REG_R0)
            self.active = None
            for r, v in zip(REGS, saved):
                uc.reg_write(r, v)
            done(r0)
            sp = uc.reg_read(arm.UC_ARM_REG_SP)
        while self.pending and self.m.nblocks >= self.pending[0][0] * self.bpm:
            ms, _seq, fire, done = self.pending.pop(0)
            got = fire(uc)
            if got is None:
                continue
            sig, body, note = got
            saved = [uc.reg_read(r) for r in REGS]
            base = (sp - 0x400) & ~7
            uc.mem_write(base, struct.pack("<HHIII", sig, 0x10 + len(body), 0, 0, 0) + body)
            mark = base - 8
            uc.reg_write(arm.UC_ARM_REG_R0, base)
            uc.reg_write(arm.UC_ARM_REG_SP, mark)
            uc.reg_write(arm.UC_ARM_REG_LR, APP_DISPATCH | 1)
            uc.reg_write(arm.UC_ARM_REG_PC, DISPATCH_SYS_SIG | 1)
            self.active = (saved, mark, done)
            note(base, saved)
            return


def queue(machine, blocks_per_ms):
    """The machine's one AppQueue (made on first use)."""
    q = getattr(machine, "app_queue", None)
    if q is None:
        q = machine.app_queue = AppQueue(machine, blocks_per_ms)
    return q
