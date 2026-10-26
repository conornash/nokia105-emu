"""ARMv5 exception entry for Unicorn.

Unicorn reports CPU exceptions to UC_HOOK_INTR and then carries on after
the instruction: it never enters the guest's handler, and for aborts it
never fills in the fault status/address registers. This module does that
work: it walks the guest's ARMv5 page tables to classify a fault, decodes
the faulting instruction to find a data abort's address, sets DFSR/DFAR,
banks LR/SPSR and jumps to the vector.

PC as seen in the hook (checked against Unicorn 2.1.4):
  SWI             the next instruction
  prefetch abort  the address that failed to fetch
  data abort      the faulting instruction
"""
import struct

import capstone
from capstone import arm as carm
from unicorn import UcError
import unicorn.arm_const as arm

EXC_SWI, EXC_PABT, EXC_DABT, EXC_IRQ, EXC_FIQ, EXC_BKPT = 2, 3, 4, 5, 6, 7

MODE_USR, MODE_FIQ, MODE_IRQ, MODE_SVC, MODE_ABT, MODE_UND = 0x10, 0x11, 0x12, 0x13, 0x17, 0x1b
CPSR_T, CPSR_F, CPSR_I = 0x20, 0x40, 0x80

# fault status codes (ARMv5 short descriptors)
FSR_ALIGN = 0x1
FSR_TRANS_SECTION, FSR_TRANS_PAGE = 0x5, 0x7
FSR_DOMAIN_SECTION, FSR_DOMAIN_PAGE = 0x9, 0xb
FSR_PERM_SECTION, FSR_PERM_PAGE = 0xd, 0xf


def cp15(uc, crn, crm=0, opc2=0, value=None):
    t = (15, 0, 0, crn, crm, 0, opc2, value or 0)
    if value is None:
        return uc.reg_read(arm.UC_ARM_REG_CP_REG, t)
    uc.reg_write(arm.UC_ARM_REG_CP_REG, t)


def _word(uc, pa):
    return struct.unpack("<I", bytes(uc.mem_read(pa, 4)))[0]


def walk(uc, va, write=False, privileged=True):
    """Translate va. Returns (pa, None) or (None, fsr) where fsr has the domain."""
    sctlr = cp15(uc, 1)
    if not sctlr & 1:
        return va, None
    ttbr = cp15(uc, 2) & 0xffffc000
    dacr = cp15(uc, 3)
    try:
        l1 = _word(uc, ttbr | (va >> 20) << 2)
    except UcError:
        return None, FSR_TRANS_SECTION
    kind = l1 & 3
    domain = (l1 >> 5) & 0xf
    if kind == 0:
        return None, FSR_TRANS_SECTION
    if kind == 2:
        pa = (l1 & 0xfff00000) | (va & 0xfffff)
        ap, section = (l1 >> 10) & 3, True
    else:
        if kind == 1:
            l2_addr = (l1 & 0xfffffc00) | ((va >> 12) & 0xff) << 2
        else:
            l2_addr = (l1 & 0xfffff000) | ((va >> 10) & 0x3ff) << 2
        try:
            l2 = _word(uc, l2_addr)
        except UcError:
            return None, FSR_TRANS_PAGE | domain << 4
        section = False
        if l2 & 3 == 0:
            return None, FSR_TRANS_PAGE | domain << 4
        if l2 & 3 == 1:    # large page, 64 KB
            pa = (l2 & 0xffff0000) | (va & 0xffff)
            ap = (l2 >> (4 + 2 * ((va >> 14) & 3))) & 3
        elif l2 & 3 == 2:  # small page, 4 KB
            pa = (l2 & 0xfffff000) | (va & 0xfff)
            ap = (l2 >> (4 + 2 * ((va >> 10) & 3))) & 3
        else:              # tiny page, 1 KB
            pa = (l2 & 0xfffffc00) | (va & 0x3ff)
            ap = (l2 >> 4) & 3
    access = (dacr >> (2 * domain)) & 3
    if access == 0 or access == 2:
        return None, (FSR_DOMAIN_SECTION if section else FSR_DOMAIN_PAGE) | domain << 4
    if access == 1 and not _ap_allows(ap, sctlr, write, privileged):
        return None, (FSR_PERM_SECTION if section else FSR_PERM_PAGE) | domain << 4
    return pa, None


def _ap_allows(ap, sctlr, write, privileged):
    if ap == 0:
        s, r = sctlr >> 8 & 1, sctlr >> 9 & 1
        return not write and ((s and privileged) or r)
    if ap == 1:
        return privileged
    if ap == 2:
        return privileged or not write
    return True


def read_virt(uc, va, size):
    out = b""
    while size:
        n = min(size, 0x1000 - (va & 0xfff))
        pa, _ = walk(uc, va)
        if pa is None:
            raise UcError(0)
        out += bytes(uc.mem_read(pa, n))
        va, size = va + n, size - n
    return out


# load/store multiple: id -> (is_write, addressing mode)
MULTI = {
    carm.ARM_INS_PUSH: (True, "db"), carm.ARM_INS_POP: (False, "ia"),
    carm.ARM_INS_LDM: (False, "ia"), carm.ARM_INS_LDMIB: (False, "ib"),
    carm.ARM_INS_LDMDA: (False, "da"), carm.ARM_INS_LDMDB: (False, "db"),
    carm.ARM_INS_STM: (True, "ia"), carm.ARM_INS_STMIB: (True, "ib"),
    carm.ARM_INS_STMDA: (True, "da"), carm.ARM_INS_STMDB: (True, "db"),
}
# single load/store: id -> (bytes, is_write)
SINGLE = {
    carm.ARM_INS_LDR: (4, False), carm.ARM_INS_LDRT: (4, False),
    carm.ARM_INS_LDRB: (1, False), carm.ARM_INS_LDRBT: (1, False),
    carm.ARM_INS_LDRSB: (1, False), carm.ARM_INS_LDRH: (2, False),
    carm.ARM_INS_LDRSH: (2, False), carm.ARM_INS_LDRD: (8, False),
    carm.ARM_INS_STR: (4, True), carm.ARM_INS_STRT: (4, True),
    carm.ARM_INS_STRB: (1, True), carm.ARM_INS_STRBT: (1, True),
    carm.ARM_INS_STRH: (2, True), carm.ARM_INS_STRD: (8, True),
    carm.ARM_INS_SWP: (4, True), carm.ARM_INS_SWPB: (1, True),
}


class ExceptionEntry:
    def __init__(self, uc, log=print):
        self.uc = uc
        self.log = log
        self.md_arm = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
        self.md_thumb = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
        self.md_arm.detail = self.md_thumb.detail = True
        self.counts = {}

    def reg(self, r):
        return self.uc.reg_read(r)

    def deliver(self, intno, pc=None):
        """Enter the guest handler for an exception: one Unicorn reported, or
        an IRQ raised by a device model (pc = the instruction it interrupts).
        Returns a description, or None if the exception isn't handled."""
        uc = self.uc
        if pc is None:
            pc = self.reg(arm.UC_ARM_REG_PC)
        cpsr = self.reg(arm.UC_ARM_REG_CPSR)
        thumb = bool(cpsr & CPSR_T)
        if intno == EXC_SWI:
            kind, mode, vector, lr, what = "swi", MODE_SVC, 0x08, pc, ""
        elif intno in (EXC_PABT, EXC_BKPT):
            kind, mode, vector, lr = "prefetch abort", MODE_ABT, 0x0c, pc + 4
            what = f"at {pc:08x}"
        elif intno == EXC_DABT:
            far, fsr, write = self.data_fault(pc, thumb)
            cp15(uc, 5, value=fsr)
            cp15(uc, 6, value=far)
            kind, mode, vector, lr = "data abort", MODE_ABT, 0x10, pc + 8
            what = f"pc {pc:08x} far {far:08x} fsr {fsr:#x}{' write' if write else ''}"
        elif intno == EXC_IRQ:
            kind, mode, vector, lr, what = "irq", MODE_IRQ, 0x18, pc + 4, ""
        else:
            return None
        self.counts[kind] = self.counts.get(kind, 0) + 1
        base = 0xffff0000 if cp15(uc, 1) & (1 << 13) else 0
        uc.reg_write(arm.UC_ARM_REG_CPSR, (cpsr & ~0x3f) | mode | CPSR_I)
        uc.reg_write(arm.UC_ARM_REG_SPSR, cpsr)
        uc.reg_write(arm.UC_ARM_REG_LR, lr)
        uc.reg_write(arm.UC_ARM_REG_PC, base + vector)
        return f"{kind} {what}".strip()

    def data_fault(self, pc, thumb):
        """Work out (far, fsr, is_write) for the data abort raised at pc."""
        md = self.md_thumb if thumb else self.md_arm
        try:
            code = read_virt(self.uc, pc, 4)
            insn = next(md.disasm(code, pc))
        except (UcError, StopIteration):
            self.log(f"data abort: cannot decode instruction at {pc:08x}")
            return pc, FSR_ALIGN, False
        lo, size, write = self.access_range(insn, pc, thumb)
        privileged = (self.reg(arm.UC_ARM_REG_CPSR) & 0x1f) != MODE_USR
        for va in [lo] + list(range((lo | 0x3ff) + 1, lo + size, 0x400)):
            _, fsr = walk(self.uc, va, write, privileged)
            if fsr is not None:
                return va, fsr, write
        self.log(f"data abort at {pc:08x} ({insn.mnemonic} {insn.op_str}): "
                 f"no fault found for {lo:08x}+{size}")
        return lo, FSR_ALIGN, write

    def access_range(self, insn, pc, thumb):
        i = insn.id
        regs = [o for o in insn.operands if o.type == carm.ARM_OP_REG]
        if i in MULTI:
            write, mode = MULTI[i]
            if i in (carm.ARM_INS_PUSH, carm.ARM_INS_POP):
                base, nregs = self.reg(arm.UC_ARM_REG_SP), len(regs)
            else:
                base, nregs = self.reg_value(regs[0].reg, pc, thumb), len(regs) - 1
            start = {"ia": base, "ib": base + 4, "da": base - 4 * nregs + 4,
                     "db": base - 4 * nregs}[mode]
            return start & 0xffffffff, 4 * nregs, write
        mem = next((o for o in insn.operands if o.type == carm.ARM_OP_MEM), None)
        if mem is None or i not in SINGLE:
            return pc, 4, False
        size, write = SINGLE[i]
        addr = self.reg_value(mem.mem.base, pc, thumb)
        if not insn.post_index:
            if mem.mem.index:
                idx = self.reg_value(mem.mem.index, pc, thumb)
                if mem.shift.type == carm.ARM_SFT_LSL:
                    idx <<= mem.shift.value
                elif mem.shift.type == carm.ARM_SFT_LSR:
                    idx >>= mem.shift.value
                elif mem.shift.type == carm.ARM_SFT_ASR:
                    idx = (idx - (1 << 32) if idx >> 31 else idx) >> mem.shift.value
                subtract = getattr(mem, "subtracted", False) or mem.mem.scale == -1
                addr += -idx if subtract else idx
            else:
                addr += mem.mem.disp
        return addr & 0xffffffff, size, write

    def reg_value(self, cs_reg, pc, thumb):
        if cs_reg == carm.ARM_REG_PC:
            return (pc + 4) & ~3 if thumb else pc + 8
        name = self.md_arm.reg_name(cs_reg)
        uc_reg = getattr(arm, f"UC_ARM_REG_{name.upper()}", None)
        if uc_reg is None:
            uc_reg = {"sb": arm.UC_ARM_REG_R9, "sl": arm.UC_ARM_REG_R10,
                      "fp": arm.UC_ARM_REG_R11, "ip": arm.UC_ARM_REG_R12}[name]
        return self.reg(uc_reg)
