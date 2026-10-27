# nokia105-emu

An emulator for the **Nokia 105 (2022, TA-1423)**, a Series 30+ feature
phone built on the UNISOC **SC6531E** (ARM926EJ-S). It boots an unmodified
dump of the phone's flash from the boot ROM onward, through the ThreadX
kernel to the home screen, and the menus can be driven by clicking the keys
of the phone drawn around the screen, or from your keyboard.

![Boot logo, home screen, main menu, Messages and the Snake menu](docs/screenshots/banner.png)

The CPU is emulated with [Unicorn](https://www.unicorn-engine.org/); the
chip's peripherals are reimplemented in Python from what the firmware
expects of them: interrupt controller, timers, SPI flash, hardware LZMA,
LCD controller and panel, the JPEG decoder and colour converter, keypad,
the PMIC (RTC, ADC, external interrupts, audio codec), the DMA controller
and voice-band codec that play sound, and enough of the DSP mailbox to
get past start-up.

**No firmware is included.** You need to dump your own phone; see
[docs/DUMPING.md](docs/DUMPING.md).

<p align="center"><img src="docs/screenshots/tour.gif" alt="Navigating the menus" width="256"></p>

## Status

Works:
- Cold boot from the boot ROM: NOKIA logo, then the home screen in about
  7.5 s of emulated time.
- Home screen with the JPEG wallpaper, a clock that runs from your
  computer's local time, and status icons.
- Keypad input: main menu, Messages, Calendar, Snake and the other menus,
  Back and the red end key.
- Display sleep and wake, and headset plug/unplug.
- Sound: ringtones, alerts, the start-up tune and keypad tones play through
  the speakers (via `ffplay`) and are saved to `out/audio.wav`. They follow
  the phone's own settings: a phone dumped in the Silent profile, as the
  TA-1423 used here was, stays quiet until you switch to General (Menu →
  Settings → Profiles → General → Activate), and keypad tones play only if
  their level (Settings → Tone settings → Keypad tones) is above zero.
- The phone keeps its settings: the live window saves the flash to
  `state/flash.bin` when it closes and boots from it next time, so a
  profile or tone change only has to be made once.
- A live window that runs at the phone's real speed: home screen about
  7.5 s after launch, menus open instantly. It shows the screen in a
  drawing of the phone whose keys you can click.
- Headless runs several times faster than real time (boot to the home
  screen in under 2 s).

Doesn't work (yet):
- No SIM, network or radio: the DSP is only a stub, so "No SIM card".
- Call audio and FM radio are silent.
- No camera or USB.
- Only tested with firmware `MOCOR_20C_6531E_W23.19.4` from a TA-1423.
  Other SC6531E phones will need changes.

## Quick start

Requirements: Python 3 with Tk for the live window (Homebrew:
`brew install python-tk`), a C compiler, and `ffplay` from ffmpeg for sound
(`brew install ffmpeg`; without it sound only goes to `out/audio.wav`). Developed and tested on macOS
with Python 3.14; Linux should work, since the Makefile builds a `.so`
there, but hasn't been tested.

```sh
git clone https://github.com/rohit746/nokia105-emu
cd nokia105-emu
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
make                          # builds the two native helper libraries
mkdir dump                    # put flash.bin and bootrom.bin here
./run.sh                      # opens the live window
```

The phone takes about 7.5 s to reach the home screen, as on real hardware.

<p align="center"><img src="docs/screenshots/window.png" alt="The live window: the home screen inside a drawing of the phone" width="232"></p>

### Controls

Click the keys on the phone in the window, or use the keyboard:

| Key | Phone |
|---|---|
| Arrow keys | navigation |
| Enter / Space | select (centre key) |
| `[` / F1 | left soft key |
| `]` / F2 / Backspace | right soft key (usually Back) |
| `c` | call (green) |
| Esc | end / power (red) |
| `0`-`9`, `*`, `#` | keypad |
| `h` | plug / unplug a headset |

## Command-line options

`run.sh` passes its options to `live.py`; `emu.py` runs headless with the
same options and saves every new screen to `out/frames/`.

| Option | |
|---|---|
| `--dump DIR` | directory with `flash.bin` and `bootrom.bin` (default `$NOKIA_DUMP`, `./dump` or `../dump`) |
| `--rtc-time 2026-10-27T10:09:00` | RTC time at power-on (default: now) |
| `--headset` | boot with a headset plugged in |
| `--mute` | don't play sound in the live window (still saved to `out/audio.wav`) |
| `--fresh` | live window: start again from the dump (the previous `state/flash.bin` is kept as `.old`) |
| `--state FILE` | keep the phone's flash in FILE (the live window uses `state/flash.bin`; headless runs start from the dump unless given this) |
| `--scale N` | window pixels per LCD pixel (`live.py`; default: the largest up to 3 that fits the screen, in steps of 0.25) |
| `--no-skin` | show only the screen, without the phone around it (`live.py`) |
| `--keys MS:KEY[:HOLD],…` | scripted key presses at emulated times, e.g. `10000:select,12000:down` |
| `--max-blocks N` | stop after N translation blocks (4000 blocks = 1 ms) |
| `--battery-mv MV`, `--no-battery`, `--adc CH=RAW` | power-supply readings |
| `--break ADDR,…`, `--trace-after BLOCK`, `--trace-addrs LO:HI` | debugging: breakpoints, MMIO trace to `out/mmio_trace.log` |
| `--slow`, `--trace-blocks A:B`, `-v` | per-block Python hook: counts unique blocks, traces blocks (much slower) |

Example, headless: boot, open the menu and save the screens:

```sh
.venv/bin/python -I emu.py --keys 10000:select,12500:down --max-blocks 60000000
```

At the end of a run `out/report.txt` lists the final registers, interrupt
counts and every peripheral register touched; `out/psram.bin` and
`out/iram.bin` are RAM dumps.

## How it works

- **Time** is counted in translation blocks: 4000 blocks are one emulated
  millisecond. A small native block hook (`blockclock.c`, registered
  through Unicorn's C API) counts them and only returns to Python when a
  timer or device event is due or an interrupt can be taken. It also keeps
  the system's millisecond and 32 kHz counters current in memory, so the
  firmware's delay loops run at native speed. When the firmware executes
  wait-for-interrupt, the emulator skips ahead to the next event; in the
  live window it is paced to the wall clock instead.
- **Interrupts** come from the device models through the interrupt
  controller; `arm_exc.py` delivers IRQs and aborts itself, walking the
  guest's page tables for faulting addresses.
- **Demand paging**: the MMI code is paged in from flash on aborts, with
  the hardware LZMA engine decompressing through `lzma_sprd.c` (Spreadtrum's
  LZMA variant).
- **Display**: the MMI draws into a framebuffer; the LCD controller model
  composes its layers and streams them to the panel model over SPI1.
- **JPEG**: the firmware programs the decoder's tables and scan data; the
  model rebuilds a standard JPEG from them and decodes it with Pillow, then
  does the hardware's colour conversion and scaling.

[docs/HARDWARE.md](docs/HARDWARE.md) has the register-level notes: memory
map, every modelled peripheral, and the firmware addresses behind them.

## Layout

| File | |
|---|---|
| `emu.py` | machine: memory map, register file, scheduler, CLI |
| `blockclock.c` | native block hook: the emulated clock |
| `live.py`, `run.sh` | live window |
| `skin.py` | the phone drawn around the screen, and where its keys are |
| `arm_exc.py` | exception entry and page-table walks |
| `irq.py` | interrupt controller and timers |
| `sfc.py`, `lzma_engine.py`, `lzma_sprd.c` | SPI flash, LZMA engine |
| `lcd.py` | LCD controller, SPI1, panel |
| `jpeg_hw.py` | JPEG decoder and colour converter |
| `keypad.py`, `eic.py`, `pmic.py` | keypad, digital EIC, PMIC (RTC, EIC) |
| `audio.py` | DMA controller and voice-band codec: sound output |
| `dsp.py` | DSP mailbox stub |
| `disasm.py`, `mkelf.py` | helpers: disassemble a range; wrap a raw image as an ELF for Ghidra |
| `third_party/lzmadec.h` | LZMA decoder from spreadtrum_flash (public domain) |

## Credits

- [spreadtrum_flash](https://github.com/ilyakurdyukov/spreadtrum_flash) and
  [fpdoom](https://github.com/ilyakurdyukov/fpdoom) by Ilya Kurdyukov: the
  dumping tools, the LZMA decoder, and register layouts for SC6531E
  peripherals that made this possible.
- [Unicorn](https://www.unicorn-engine.org/),
  [Capstone](https://www.capstone-engine.org/) and
  [Pillow](https://python-pillow.org/).

## Legal

This project contains no firmware or code from Nokia, HMD Global or UNISOC.
It was written by observing how the phone's firmware uses its hardware.
Nokia is a trademark of Nokia Corporation; this project is not affiliated
with or endorsed by Nokia, HMD Global or UNISOC. Screenshots show the
phone's own interface, running in the emulator; the phone around the
screen in the live window is drawn by `skin.py`. Only use firmware you
dumped from a phone you own.

## License

MIT, see [LICENSE](LICENSE). `third_party/lzmadec.h` is public domain.
