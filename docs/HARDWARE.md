# SC6531E hardware notes

What the emulator models, as worked out from the Nokia 105 TA-1423
firmware (`MOCOR_20C_6531E_W23.19.4`). Addresses in the firmware are
written as `flash 0x…` (raw flash image), `kern 0x…` (kernel, decompressed
at 0x04000000) and `user 0x…` (MMI code, demand-paged at 0x08000000). The
module docstrings and code comments cite the firmware code behind each
register; this page is the overview. Register layouts marked *inferred*
were deduced from how the firmware uses them, not from documentation.

## CPU and memory

ARM926EJ-S (ARMv5TE) running ThreadX-based MOCOR. Unicorn emulates the
core; `arm_exc.py` delivers exceptions itself, because Unicorn neither
vectors to the guest's handlers nor sets DFAR/DFSR on this core.

| Address | Size | What |
|---|---|---|
| `0x00000000` | 0x16000 | boot ROM until the remap, then a flash alias |
| `0x04000000` | 4 MB | PSRAM: kernel, heap, framebuffers |
| `0x08000000` | | MMI code and resources, demand-paged from flash |
| `0x10000000` | 4 MB | memory-mapped SPI NOR flash |
| `0x40000000` | 64 KB | internal SRAM |

`0x205000e0`: bit 0 = boot ROM at address 0 (cleared by the boot ROM's
last act), bit 1 must be set for a normal boot (else download mode).

Demand paging: 0x08000000 is mapped lazily. Each 4 KB chunk of the MMI
image is a separate stream in Spreadtrum's LZMA variant, and the abort
handler has the hardware LZMA engine decode the 1 KB slice holding the
faulting address (the engine's windowed mode).

## Peripherals

| Block | Address | IRQ | Module |
|---|---|---|---|
| Interrupt controller (2 banks) | `0x80000000`, `0x80001000` | | `irq.py` |
| Timers (32 kHz / 26 MHz) | `0x81000000`, `0x81001000` | 4, 5, 6 | `irq.py` |
| System counter (ms at `+0x300c`) | `0x81003000` | | `emu.py` |
| SPI flash controller | `0x20a00000` | | `sfc.py` |
| LZMA decompressor | `0x20e00000` | | `lzma_engine.py` |
| JPEG decoder + colour converter | `0x20200000`, `0x20210000` | 38 | `jpeg_hw.py` |
| LCD controller | `0x20d00000` | 22 | `lcd.py` |
| SPI1 (LCD panel bus) | `0x69000000` | | `lcd.py` |
| Keypad | `0x87000000` | 12 | `keypad.py` |
| Digital EIC (debounced inputs) | `0x8a001000` | 11 | `eic.py` |
| SIM card interface (`--sim`) | `0x89000000` | 17 | `sim.py` |
| ADI bus to the PMIC | `0x82000000` | | `emu.py` |
| PMIC (SC1161): RTC, EIC, ADC | `0x82001000` (via ADI) | 36 | `pmic.py`, `emu.py` |
| DSP shared memory / mailbox | `0x30000000` | 21 | `dsp.py` |
| DMA controller | `0x20100000` | 31 | `audio.py` |
| GPIO (torch) | `0x8a000000` | | `lights.py` |
| eFuse (ADC calibration) | `0x82002000` | | `battery.py` |
| Voice-band codec (DAC FIFOs) | `0x82003000` | | `audio.py` |

Interrupt controller banks: `+0x00` masked status, `+0x04` raw, `+0x08`
enable (write 1 to set), `+0x0c` disable, `+0x2c` FIQ disable (*inferred*:
the firmware writes it just before enabling a line). IRQ n is
bank n / 32, bit n % 32. The kernel's handler table (RAM `0x42f90f0`) has
24-byte records: id, bank, bit mask, handler, deferred handler, default.
PMIC interrupts arrive on line 36 and are split by the source bits in
`0x82001020` into virtual lines 66 (RTC) and 69 (EIC).

### PMIC (through the ADI bus)

Reads go through the ADI: write the register offset to `0x82000008`, read
`0x8200000c` (offset << 16 | value).

- Chip id `0x82001400`/`04` = `0xa000`/`0x1161`.
- ADC `0x82001300`: channel register `+0x04` (channel | `0x20` scale | `0x40`),
  result `+0x4c`, done `+0x5c` bit 0. Register value `0x62` is the battery
  voltage (mV = 690 + 1.25 × raw, must be ≥ 3380 to boot); `0x60` is a
  periodic check (flash `0x1b636`) whose converted value must stay in
  985..1058, else the phone shows a warning and powers off after 3 s. It
  is probably the battery temperature (*inferred*).
- RTC: counters `0x82001180`..`8c` (seconds, minutes, hours, days since
  2000-01-01), set registers `0x82001190`..`9c`, alarm registers
  `0x820011a0`..`ac` (same fields), interrupt enable `0xb0`, status `0xb4`
  (bit 1 = minute tick, bit 4 = alarm, bits 8-11 = time set done, 12-15 =
  alarm set done), clear `0xb8`. The driver waits for the set-done bits
  (flash `0x16e12`) and stops the phone with an assertion when they don't
  come. The alarm fires when the counters reach the alarm registers; the
  handler (flash `0x16cbe`) clears its enable bit and passes it on as MMI
  event `0x72`, which shows the alarm screen. At boot the firmware programs
  the next alarm saved in its settings, or a time long past.
- Backlights (fpdoom's `ANA_WHTLED_CTRL`): `0x8200153c` is the screen, bit
  0 = off, bits 1-4 = level (Intensity 1-7, default 4); `0x82001540` is
  the keypad, bit 11 = off, bits 12-15 = level. Both step through their
  levels 10 ms apart when switched.
  The high byte of spare register `0x820011d0` must be `0xbb` or boot
  resets the clock to 2023-01-01 (flash `0x1727c`).
- EIC `0x82001200`: data `+0x00`, polarity `+0x14`, enable `+0x18`, raw
  `+0x1c`, masked `+0x20`. Inputs: bit 0 = charger, bit 1 = power key
  (pressed = 1), bit 8 = battery present.

### Digital EIC (`0x8a001000`)

Same layout as the PMIC EIC, plus `+0x04` data mask, `+0x24` clear and
`+0x28` re-arm. Input 1 is headset detect, active low (low = plugged in).
The kernel's input table (RAM `0x430a060`, 16 bytes per logical input:
id, type, active level, line, callback) maps logical inputs to EIC lines;
the line numbering is the controller list at flash `0x2d108`
(`0x8a001000` lines 0-15, PMIC EIC 16-31, then three 8-line groups).

### Keypad (`0x87000000`)

`+0x08` raw interrupt: bits 0-3 key down in slot 0-3, bits 4-7 key up;
`+0x10` clear; `+0x2c` key status, one byte per slot: bits 0-2 =
keymap index / 5, bits 4-6 = index % 5. The keymap is 40 key codes,
`*` 6 9 # · 2 5 8 0 · 1 4 7 3 · call lsoft left up · select down right
rsoft. The red key is the power key on the PMIC EIC.

### Display

The panel is an ST7735-style controller on SPI1 (`0x69000000`, register
layout from fpdoom). The LCD controller (`0x20d00000`) composes an image
layer and three OSD layers and streams the result to SPI1. The MMI draws
in software into an RGB565 framebuffer (`0x0415fb00`) shown through OSD
layer 1, and refreshes partial windows.

LCDC raw interrupt (`+0x11c`) bit 0 = refresh done, bit 1 = the panel's
tearing-effect signal: before each refresh the driver polls bit 1 for up
to 2 s (kern `0x40220ec`), so the emulator always reports it.

### JPEG decoder (`0x20200000` / `0x20210000`)

The firmware parses JPEG headers itself and programs the hardware with
quantisation tables (`+0x300`, bit-reversed order), Huffman symbols
(`0x20202000`), image size (`+0x0c`), output buffers (`+0x40`..`+0x4c`)
and the entropy-coded scan (`+0x50`). Writing bit 31 of `0x20210400`
starts the decode; output comes in bands of rows through
`0x20211808`..`18`, with a one-hot output-pair select. The emulator
rebuilds a baseline JPEG from that state and decodes it with Pillow.

The same block then converts the YCbCr result to RGB565, cropping and
scaling (`ctrl` bit 1, source rectangle `+0x134`/`+0x138`, output size
`+0x10`, output address `+0x50`, start `+0x64`), and signals completion
with interrupt bit 4 on line 38. If that interrupt never comes, the MMI
waits 3 s and converts in software.

### Sound

The MMI synthesises ringtones on the ARM (thread `T_MIDI`, 22050 Hz) into a
FIFO; a mixer (user `0x822bf20`) and the kernel's audio HAL (descriptor
`AUDIO_HAL` at RAM `0x4390f80`) turn that into 48 kHz stereo in a ring
buffer, and DMA channels 4 and 5 copy 320-sample blocks from the ring to the
voice-band codec's DAC FIFOs at `0x82003000` (left) and `0x82003004`
(right). The DAC runs at 32 kHz: measured from the pitch of Nokia Tune,
which plays exactly 1.5x sharp at 48 kHz (no register selecting the rate
was found; the firmware resamples its 22.05 kHz synthesiser output in
software). The DMA controller's channels sit at `0x20101000 + 0x40 * n`
(Linux sprd-dma layout); the hardware clears a channel's enable bit when a
block is done, and IRQ 31 reports it.

The power-up also waits on an analogue control block at `0x8a002000`
(IRQ 16): after starting a ramp through bit 14 of `+0x0c`, the state
machine enables the block's interrupts (`+0x28` bits 0-1) and arms a
600 ms timer; status `+0x20` bit 5 ends the wait early (its deferred
handler, kern `0x4066ffc`, runs the timer's callback). The emulator raises
it 20 ms after it is enabled; without it every sound starts 600 ms late.

Before any of this runs, an audio power state machine (kern `0x40670fa`,
handler table at `0x40a59d8`) powers the codec up. Its calibration step
(kern `0x402b476`) writes `0x6006` then `0xe006` to the codec register
`0x820013d4` and waits for bits 12 and 10; without them the ARM data path
(bit 12 of the HAL flags) is never enabled and the DMA plays its silence
buffer (`0x4390d00`). A later step (kern `0x402b538`), first needed by the
alarm tone, sets bits 8, 5 and 7 of the same register and polls for bits 4
and 3 with no timeout; without them `SPRD_CODEC_TASK` spins forever and,
being of higher priority, starves the MMI. The mixer drops the samples when the active profile
mutes ringtones (Silent). Keypad tones take the same path.

### Flash and saved state

The firmware keeps its settings, contacts and messages in the NV area of
the flash (around `0x398000`-`0x3c0000`), written through the SPI flash
controller, as soon as a setting changes (a burst of programs and erases
within a few milliseconds). The flash model applies those writes to its
in-memory image; `--state FILE` saves that image 2 s after the firmware
last wrote it, and again at the end of a run, and boots from it next time,
with `FILE.source` holding the SHA-256 of the dump it came from.

### Battery and charger

The battery is ADC channel 2 (`0x62`, 690 mV + 1.25 mV per step with the
calibration below). The charge manager (thread `CHGMNG_SRV`, message switch
at flash `0x1baa6`) turns it into a level through a 12-point table (flash
`0x1b494`); about 3.6 V gives Battery low, 3.5 V Low battery, and 3.4 V
Battery empty and a power-off.

Plugging the charger in raises PMIC EIC input 0 (kernel input 7, callback
kern `0x40008e8`), but the handler (flash `0x1bdfe`) ignores the level and
reads the charger status `0x8200155c`: bit 2 = charger present (flash
`0x212ac`); without it the phone says the battery is not fully charged, as
when unplugged. While charging, a check every 500 ms (flash `0x1b760`):

- measures the charger voltage on ADC channel 3 (`0x63`) on the small
  scale, times 1000/74 (flash `0x1b58a`); four readings under 4300 mV in a
  row mean unplugged;
- ends the charge when status bit 4 (flash `0x212a2`) has been set for ten
  checks (reason 4: "Battery full. Unplug charger from socket.");
- stops on status bit 1 or 0 (flash `0x212b6`, `0x212c0`) as a fault
  (reason 5: "Not charging").

Bit 0 of `0x82001550` switches charging off (flash `0x210da`).

The small scale has to be calibrated, from the eFuse (`0x82002000`: block
number written to both halves of `+0x08`, data at `+0x00`). The kernel
builds the ADC calibration (kern `0x4015728`, record at RAM `0x42fbf44`):
the battery scale from block 5 bits 28-21 and 20-13 (flash `0x1df08`; 0
gives the 1.25 mV per step above), the small scale from block 5 bits 12-5,
which must be at least 46, and block 6 bits 29-22 (flash `0x1df66`). With
the eFuse all zero the small scale stays uncalibrated, every charger
reading converts to 0 mV, and the charger is taken out within 2 s; the
emulator's eFuse puts 1000 mV at raw 3276 and 100 mV at raw 328.

Started by the charger (power key not held), the phone boots into
charging-only mode, but first the kernel (kern `0x40263cc`) waits up to
15 s for a PC on the USB/serial port, cut short only by the charger going
or the power key.

### GPIO and lights (`0x8a000000`)

Banks of 16 pins, 0x80 apart: `+0x00` data, `+0x04` data mask, `+0x08`
direction (1 = output), `+0x18` interrupt sense (fpdoom's layout). Bank 0
pin 0 is the torch LED: Up twice on the home screen toggles it, holding Up
lights it while held, and the factory test menu (`#*8378#0#`, Item test)
has a torch test. The other outputs are set once at boot.

The factory menu's vibrator test switches the audio path on and off once a
second (the same PMIC and AUD writes as any sound) and writes the codec
registers `0x82003088`..`ac`, but nothing else; with Vibrating alert on,
the alarm does not even do that. No vibration motor control was found.

### DSP

The DSP (GSM baseband) shares memory at `0x30000000`; the ARM writes
commands at `0x30000000` and the DSP acknowledges in `0x30000002`. The
emulator only acknowledges and raises the "booted" interrupt (line 21), so
there is no radio.

## Boot requirements

A normal boot powers off unless:

1. the power key is held until a check about 1.3 s after reset
   (`--power-key-ms`, default 3000),
2. battery detect (PMIC EIC bit 8) is high,
3. the battery voltage reads ≥ 3380 mV,
4. the periodic ADC check reads inside its window.

Useful breakpoints when the phone shuts down: user `0x080034b8` (message
then 3 s power-off timer), kernel `0x0400425e` (power down), flash
`0x10e0a` (final loop).

## Switching off

Holding the red key opens the power menu, and holding on chooses Switch
off!: the goodbye screen, then flash `0x10d5c` sets the power-down bit of
each supply (`0x82001438`..`9c`), unlocks `0x82001588` with `0x6e7f` and
writes `0x1f` to `0x82001418`, and spins at `0x10e0a`. Nothing else writes
`0x82001418`, so the emulator takes that write as the chip cutting its
power and ends the run. Switching on is a cold boot with the power key
held, from the flash as it was saved.
