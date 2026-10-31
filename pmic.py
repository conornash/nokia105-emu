"""Power-chip (SC1161-style) interrupt sources: the RTC and the EIC.

The firmware's RTC driver (flash 0x16c54..0x17160, state at 0x42f9c5c):
  0x82001180/84/88/8c  current seconds / minutes / hours / days (read via ADI)
  0x82001190/94/98/9c  set seconds / minutes / hours / days (0x16ee2..)
  0x820011a0/a4/a8/ac  alarm seconds / minutes / hours / days
  0x820011b0           interrupt enable (the firmware leaves 0xff02, 0xff12
                       while an alarm is set)
  0x820011b4           event status: bit 1 = minute tick, bit 4 = alarm (the
                       handler, flash 0x16cbe, turns the alarm into MMI
                       event 0x72 and clears its enable bit), bits 8-11 =
                       that time field's update done, 12-15 alarm field's
  0x820011b8           write 1s to clear status bits
After writing the time it marks bits 8-11 pending and busy-waits ~2 ms for
the RTC interrupt handler (irq 66, registered as 0x16cbe) to clear them.
Days count from 2000-01-01 (the firmware's default time is day 0x20d1,
2023-01-01). The counters run from --rtc-time, the host's local time by
default.

The EIC (external interrupt controller) at 0x82001200 turns input lines
into interrupts: bit 1 is the power key, bit 8 battery detect (see
Machine.eic_inputs). Its handler (irq 69, kern 0x403e4b0) checks +0x20
per channel, acknowledges, and calls the channel's callback with the
level from +0x00 (kern 0x403e2b2/0x403e26c). Channels are taken to be
level-triggered: pending while enabled (+0x18) and level == polarity
(+0x14); the driver flips the polarity to wait for the opposite edge.

Power-chip interrupts reach the ARM as INTC bank 1 bit 4 (irq 36); the
dispatcher (flash 0x1666e) then reads the source bits from 0x82001020,
where bit 2 is the RTC (irq 66) and bit 5 the EIC (irq 69).

Switching off (holding the red key, then Switch off!) ends in flash
0x10d5c: it sets the power-down bit of each supply (0x82001438..9c),
unlocks 0x82001588 with 0x6e7f and writes 0x1f to 0x82001418, then spins
at 0x10e0a. 0x82001418 is written nowhere else, so that write is taken as
the chip cutting its own power.
"""
import datetime

RTC_COUNT = (0x82001180, 0x82001184, 0x82001188, 0x8200118c)
RTC_SET = (0x82001190, 0x82001194, 0x82001198, 0x8200119c)
RTC_ALARM = (0x820011a0, 0x820011a4, 0x820011a8, 0x820011ac)
RTC_INT_EN = 0x820011b0
RTC_INT_MIN = 1 << 1
RTC_INT_ALARM = 1 << 4
RTC_SET_DONE = 0xff00      # bits 8-11 time set done, 12-15 alarm set done
RTC_EPOCH = datetime.datetime(2000, 1, 1)
UNIT_SECONDS = (1, 60, 3600, 86400)
UNIT_MODULO = (60, 60, 24, 1 << 16)
RTC_INT_STS = 0x820011b4
POWER_DOWN = 0x82001418
RTC_INT_CLR = 0x820011b8
ANA_INT_STS = 0x82001020
ANA_IRQ_LINE = 36          # INTC bank 1 bit 4
ANA_INT_RTC = 1 << 2
ANA_INT_EIC = 1 << 5
EIC_BASE = 0x82001200
EIC_DATA, EIC_IEV, EIC_IE, EIC_RIS, EIC_MIS = 0x00, 0x14, 0x18, 0x1c, 0x20
RTC_LATCH_BLOCKS = 4000    # about 1 ms of emulated time


class Pmic:
    def __init__(self, machine, start):
        self.m = machine
        self.rtc_status = 0
        self.rtc_events = 0
        # RTC time (seconds since RTC_EPOCH) at emulated time zero
        self.rtc_base = int((start - RTC_EPOCH).total_seconds())
        self.rtc_generation = 0
        self.alarm = [0] * 4            # seconds, minutes, hours, days
        self.alarm_generation = 0
        self.schedule_minute()

    def seconds_per_block(self):
        return 1 / (1000 * self.m.blocks_per_ms)

    def rtc_now(self):
        return self.rtc_base + int(self.m.nblocks * self.seconds_per_block())

    def rtc_read(self, index):
        return self.rtc_now() // UNIT_SECONDS[index] % UNIT_MODULO[index]

    def blocks_until(self, seconds):
        """Blocks until the RTC reads `seconds`."""
        return (seconds - self.rtc_base) * 1000 * self.m.blocks_per_ms - self.m.nblocks

    def schedule_minute(self):
        self.rtc_generation += 1
        gen = self.rtc_generation
        now = self.rtc_now()
        self.m.schedule(max(1, self.blocks_until(now - now % 60 + 60)),
                        lambda: self.minute_tick(gen))

    def minute_tick(self, gen):
        if gen != self.rtc_generation:
            return
        self.rtc_event(RTC_INT_MIN)
        self.schedule_minute()

    def on_rtc_set(self, index, value):
        now = self.rtc_now()
        field = now // UNIT_SECONDS[index] % UNIT_MODULO[index]
        self.rtc_base += ((value & 0xffff) - field) * UNIT_SECONDS[index]
        self.schedule_minute()
        self.schedule_alarm()
        bit = 1 << (8 + index)
        self.m.schedule(RTC_LATCH_BLOCKS, lambda: self.rtc_event(bit))

    def on_alarm_set(self, index, value):
        """Alarm registers latch like the time ones and acknowledge with bits
        12-15 (the driver waits for them, flash 0x16e12, then asserts)."""
        bit = 1 << (12 + index)
        self.m.schedule(RTC_LATCH_BLOCKS, lambda: self.rtc_event(bit))
        self.alarm[index] = value & 0xffff
        self.schedule_alarm()

    def shift(self, seconds):
        """Move the RTC by `seconds` (the emulator's own correction, not the
        firmware setting the time)."""
        if seconds:
            self.rtc_base += seconds
            self.schedule_minute()
            self.schedule_alarm()

    def clock(self):
        """The RTC's date and time now."""
        return RTC_EPOCH + datetime.timedelta(seconds=self.rtc_now())

    def alarm_seconds(self):
        return sum(v * u for v, u in zip(self.alarm, UNIT_SECONDS))

    def schedule_alarm(self):
        """The alarm fires when the counters reach the alarm registers; an
        alarm time already past never fires."""
        self.alarm_generation += 1
        gen = self.alarm_generation
        blocks = self.blocks_until(self.alarm_seconds())
        if blocks > 0:
            self.m.schedule(blocks, lambda: self.alarm_due(gen))

    def alarm_due(self, gen):
        if gen == self.alarm_generation:
            print(f"[block {self.m.nblocks}] rtc: alarm {RTC_EPOCH + datetime.timedelta(seconds=self.alarm_seconds())}")
            self.rtc_event(RTC_INT_ALARM)

    def rtc_event(self, bits):
        self.rtc_status |= bits
        self.rtc_events += 1
        self.update()

    def on_rtc_clr(self, value):
        self.rtc_status &= ~value
        self.update()

    def eic_raw(self):
        level = self.m.eic_inputs()
        iev = self.m.regs.get(EIC_BASE + EIC_IEV, 0)
        return ~(level ^ iev) & 0xffff

    def eic_masked(self):
        return self.eic_raw() & self.m.regs.get(EIC_BASE + EIC_IE, 0)

    def rtc_pending(self):
        # update-done events always interrupt; ticks only when enabled
        return self.rtc_status & (RTC_SET_DONE | self.m.regs.get(RTC_INT_EN, 0))

    def read_ana_int(self):
        return (ANA_INT_RTC if self.rtc_pending() else 0) | (ANA_INT_EIC if self.eic_masked() else 0)

    def update(self):
        self.m.intc.set_line(ANA_IRQ_LINE, self.read_ana_int())
