/* Native block hook: the emulated clock.
 *
 * Unicorn calls a block hook before every translation block. Doing that
 * in Python costs far more than emulating the block, so this hook only
 * counts blocks (the emulator's time base) and stops emulation when the
 * Python side has work: a timer or device event is due (deadline), an
 * interrupt can be taken (irq_pending with CPSR.I clear), or a block
 * keeps re-entering itself (a stuck loop). It also keeps the system
 * counter page (0x81003000: ms counter at +0x0c, 32 kHz counter at +0x04)
 * current in host memory mapped into the guest, so the firmware's many
 * time reads (delay loops) need no MMIO callback. emu.py registers it with
 * uc_hook_add through ctypes and shares struct clock with it; Unicorn's
 * uc_emu_stop and uc_reg_read are passed in as pointers, so no Unicorn
 * headers are needed.
 *
 * build: make
 */
#include <stdint.h>

typedef int (*emu_stop_fn)(void *uc);
typedef int (*reg_read_fn)(void *uc, int regid, void *value);

enum {
	STOP_DEADLINE = 1,
	STOP_IRQ = 2,
	STOP_SPIN = 4,
};

#define CPSR_I 0x80

struct clock {
	uint64_t nblocks;      /* blocks executed (or skipped) so far */
	uint64_t deadline;     /* stop when nblocks reaches this */
	uint64_t last_addr;    /* address of the previous block */
	uint32_t irq_pending;  /* an enabled interrupt line is raised */
	uint32_t reason;       /* STOP_* bits since Python last cleared them */
	uint32_t spin;         /* times last_addr re-entered itself */
	uint32_t spin_limit;
	int32_t cpsr_id;
	uint32_t blocks_per_ms;
	emu_stop_fn emu_stop;
	reg_read_fn reg_read;
	volatile uint32_t *counters;  /* guest page 0x81003000, or NULL */
	uint64_t next_ms;      /* nblocks at which the counters next change */
	uint64_t next_tick;
};

static void update_counters(struct clock *c) {
	uint64_t n = c->nblocks, per_s = 1000ull * c->blocks_per_ms;
	if (n >= c->next_ms) {
		uint64_t ms = n / c->blocks_per_ms;
		c->counters[3] = (uint32_t)ms;
		c->next_ms = (ms + 1) * c->blocks_per_ms;
	}
	if (n >= c->next_tick) {
		uint64_t tick = n * 32768 / per_s;
		c->counters[1] = (uint32_t)tick;
		c->next_tick = ((tick + 1) * per_s + 32767) / 32768;
	}
}

void clock_block_hook(void *uc, uint64_t addr, uint32_t size, struct clock *c) {
	uint32_t stop = 0;
	(void)size;
	c->nblocks++;
	if (c->counters)
		update_counters(c);
	if (addr == c->last_addr) {
		if (++c->spin == c->spin_limit)
			stop |= STOP_SPIN;
	} else {
		c->last_addr = addr;
		c->spin = 0;
	}
	if (c->nblocks >= c->deadline)
		stop |= STOP_DEADLINE;
	if (c->irq_pending) {
		uint32_t cpsr = 0;
		c->reg_read(uc, c->cpsr_id, &cpsr);
		if (!(cpsr & CPSR_I))
			stop |= STOP_IRQ;
	}
	if (stop) {
		c->reason |= stop;
		c->emu_stop(uc);
	}
}
