# Dumping the firmware

The emulator needs two images from your own phone. No firmware is included
in this repository: it is copyrighted, and a dump also holds data specific
to your handset (IMEI, calibration), so **do not publish your dump**.

| File | What | Size |
|---|---|---|
| `flash.bin` | the whole 4 MB NOR flash | 0x400000 |
| `bootrom.bin` | the SC6531E boot ROM, read from address 0 | 0x16000 |

Put them in `dump/` next to `emu.py`, or anywhere else and pass
`--dump DIR` (or set `NOKIA_DUMP`).

This was done on a **Nokia 105 (2022), TA-1423**, firmware
`MOCOR_20C_6531E_W23.19.4_Release`. Other SC6531E phones use the same
procedure but a different boot key, and the emulator has only been tested
with this firmware.

## 1. Build spd_dump and the custom FDL

Use [spreadtrum_flash](https://github.com/ilyakurdyukov/spreadtrum_flash)
by Ilya Kurdyukov:

```sh
git clone https://github.com/ilyakurdyukov/spreadtrum_flash
cd spreadtrum_flash
make    # builds spd_dump; needs libusb (brew install libusb / apt install libusb-1.0-0-dev)
```

`spd_dump` sends a small loader (FDL) to the phone's boot ROM over USB and
then reads memory through it. SC6531E phones need the custom
`nor_fdl1.bin`: take the prebuilt one from the project's Releases page, or
build it with an ARM cross compiler (see its `custom_fdl/README.md`).

On macOS the FDL builds with Homebrew LLVM. The freestanding build has no
`string.h`, so give it a header that declares `memcpy`/`memset`:

```sh
mkdir -p /tmp/fdl_include
cat > /tmp/fdl_include/string.h <<'EOF'
#include <stddef.h>
void *memcpy(void *dst, const void *src, size_t n);
void *memset(void *dst, int c, size_t n);
EOF
make -C custom_fdl CHIP=1 \
  CC="/opt/homebrew/opt/llvm/bin/clang --target=armv5te-none-eabi -fuse-ld=/opt/homebrew/bin/ld.lld" \
  OBJCOPY=/opt/homebrew/opt/llvm/bin/llvm-objcopy EXTRA_CFLAGS=-I/tmp/fdl_include
cp custom_fdl/nor_fdl1.bin .
```

Optional: [spd_dump-check-baud-retry.patch](spd_dump-check-baud-retry.patch)
makes `spd_dump` retry the first handshake, which was sometimes lost right
after the phone enumerated. Apply it with `git apply` before `make`.

## 2. Put the phone in download mode

1. Unplug the USB cable and take the battery out for about 10 seconds.
2. Put the battery back.
3. Hold the **boot key** (`*` on the TA-1423) and plug the USB cable in.

In download mode the phone shows up as USB `1782:4d00` (in normal or
charging mode it is `1782:3d00`). Holding the wrong key can still show
`4d00` for a few seconds, but the boot ROM never answers; if `spd_dump`
fails with `ver expected`, try again with another key.

## 3. Read flash and boot ROM

Start the command first, then connect the phone as above:

```sh
./spd_dump --wait 600 end_data 0 \
    fdl nor_fdl1.bin 0x40004000 \
    read_flash 0x80000003 0 0x400000 flash.bin \
    read_mem 0 0x16000 bootrom.bin
```

`end_data 0` is needed on this phone. Copy both files into the emulator's
`dump/` directory.

## Optional: unpack the firmware

For reverse engineering, `fphelper` from the same repository unpacks the
compressed images (kernel, user code, resources, keymap):

```sh
make -C fphelper
./fphelper/fphelper flash.bin unpack    # see fphelper's README
```

Put the output in `dump/unpacked/`. The emulator only uses
`unpacked/keymap.bin`, and falls back to the TA-1423 layout without it.
