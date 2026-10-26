"""SPI1 controller (0x69000000) and the ST7735-style LCD panel behind it.

Register layout from fpdoom's lcd_spi.h:
  +0x00 txd      data in and out (16-bit frames: command << 8 | reply)
  +0x28 int_raw  bit 8 TX end
  +0x34 sts2     bit 5 RX FIFO empty, bit 7 TX FIFO empty, bit 8 busy
  +0x54 ctl8     bit 15: 0 command, 1 data
Transfers finish instantly, so status always reads idle with TX done.

The panel answers the ID reads (0xda/0xdb/0xdc) and draws pixel data
written after RAMWR into a 128x160 RGB565 framebuffer, saved as PNG.

The LCD controller (LCDC, 0x20d00000, layout from fpdoom's lcdc_base_t)
composes the frame from its image layer and three OSD layers (RGB565 only,
with block alpha; colour key not yet) and streams it to cap.base_addr,
here SPI1's txd, when ctrl bit 3 is set.
"""
from array import array
import struct
import zlib

SPI1_BASE = 0x69000000
TXD = 0x00
INT_RAW = 0x28
STS2 = 0x34
CTL8 = 0x54

INT_TX_END = 1 << 8
STS2_TXF_EMPTY = 1 << 7
CTL8_DATA = 1 << 15

WIDTH, HEIGHT = 128, 160

CASET, RASET, RAMWR, MADCTL, COLMOD = 0x2a, 0x2b, 0x2c, 0x36, 0x3a
TEOFF, TEON = 0x34, 0x35
READ_ID = {0xda: 0, 0xdb: 1, 0xdc: 2}


class Panel:
    def __init__(self, panel_id, log):
        self.id_bytes = panel_id.to_bytes(3, "big")
        self.log = log
        self.fb = array("H", bytes(WIDTH * HEIGHT * 2))
        self.cmd = None
        self.args = []
        self.x0, self.x1, self.y0, self.y1 = 0, WIDTH - 1, 0, HEIGHT - 1
        self.x = self.y = 0
        self.pending = None   # high byte of a pixel split over 8-bit writes
        self.pixels = 0
        self.dirty = False
        self.te_on = False      # tearing-effect (frame sync) output enabled

    def command(self, cmd):
        self.cmd, self.args, self.pending = cmd, [], None
        if cmd == RAMWR:
            self.x, self.y = self.x0, self.y0
        elif cmd in (TEON, TEOFF):
            self.te_on = cmd == TEON
        self.log(f"lcd cmd {cmd:02x}")

    def data(self, value, bits):
        if self.cmd == RAMWR:
            if bits >= 16:
                self.pixel(value & 0xffff)
            elif self.pending is None:
                self.pending = value & 0xff
            else:
                self.pixel(self.pending << 8 | (value & 0xff))
                self.pending = None
            return
        self.args.append(value & 0xff)
        if self.cmd in (CASET, RASET) and len(self.args) == 4:
            lo, hi = self.args[0] << 8 | self.args[1], self.args[2] << 8 | self.args[3]
            if self.cmd == CASET:
                self.x0, self.x1 = lo, hi
            else:
                self.y0, self.y1 = lo, hi
        if len(self.args) <= 4:
            self.log(f"lcd data {value & 0xffff:04x} (cmd {self.cmd if self.cmd is not None else -1:02x})")

    def pixel(self, rgb565):
        if self.x < WIDTH and self.y < HEIGHT:
            self.fb[self.y * WIDTH + self.x] = rgb565
            self.dirty = True
        self.pixels += 1
        self.x += 1
        if self.x > self.x1:
            self.x = self.x0
            self.y = self.y + 1 if self.y < self.y1 else self.y0

    def reply(self, cmd):
        i = READ_ID.get(cmd)
        return self.id_bytes[i] if i is not None else 0

    def stream(self, values):
        """Pixels arriving after RAMWR from the LCDC, a whole frame at a time."""
        if self.cmd != RAMWR:
            self.log(f"lcd: {len(values)} streamed pixels outside RAMWR (cmd {self.cmd})")
            return
        w = self.x1 - self.x0 + 1
        for i in range(0, len(values), w):
            if self.y < HEIGHT:
                n = min(w, WIDTH - self.x0, len(values) - i)
                start = self.y * WIDTH + self.x0
                self.fb[start:start + n] = values[i:i + n]
            self.y = self.y + 1 if self.y < self.y1 else self.y0
        self.pixels += len(values)
        self.dirty = True

    def save_png(self, path):
        rows = bytearray()
        for y in range(HEIGHT):
            rows.append(0)
            for x in range(WIDTH):
                v = self.fb[y * WIDTH + x]
                r, g, b = v >> 11, (v >> 5) & 0x3f, v & 0x1f
                rows += bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))

        def chunk(kind, body):
            c = kind + body
            return struct.pack(">I", len(body)) + c + struct.pack(">I", zlib.crc32(c))
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b"")
        path.write_bytes(png)
        self.dirty = False


class Spi1:
    def __init__(self, machine, panel):
        self.m = machine
        self.panel = panel
        self.rx = 0

    def reg(self, off):
        return self.m.regs.get(SPI1_BASE + off, 0)

    def read_txd(self):
        return self.rx

    def on_txd(self, value):
        bits = 16 if value >> 8 else 8
        if not self.reg(CTL8) & CTL8_DATA:
            cmd = value >> 8 if value >> 8 else value & 0xff
            if value >> 8 and (value >> 8) in READ_ID:
                # 16-bit read frame: command out in the high byte, reply in the low
                self.rx = value & 0xff00 | self.panel.reply(value >> 8)
                return
            self.panel.command(cmd)
            self.rx = value
        else:
            self.panel.data(value, bits)
            self.rx = value


LCDC_BASE = 0x20d00000
LCDC_CTRL = 0x00
LCDC_BG_COLOR = 0x10
LCDC_LCM_SIZE = 0x0c
IMG_CTRL = 0x20
IMG_BASE = 0x24
IMG_SIZE = 0x2c
IMG_PITCH = 0x30
IMG_DISP_XY = 0x34
OSD_BASES = (0x50, 0x80, 0xb0)
# offsets within an OSD layer block
OSD_CTRL, OSD_ADDR, OSD_SIZE, OSD_PITCH, OSD_DISP_XY, OSD_ALPHA = 0x00, 0x04, 0x0c, 0x10, 0x14, 0x18
LAYER_ENABLE, LAYER_COLOR_KEY, LAYER_BLOCK_ALPHA = 1 << 0, 1 << 1, 1 << 2
CAP_BASE = 0xe4
IRQ_EN, IRQ_CLR, IRQ_STATUS, IRQ_RAW = 0x110, 0x114, 0x118, 0x11c

CTRL_REFRESH = 1 << 3
IRQ_REFRESH_DONE = 1 << 0
# Raw bit 1 is taken to be the panel's tearing-effect (frame sync) input:
# before a refresh kern 0x40220ec (type 3) polls it every 10 ms for up to
# 2 s, also when the boot-time init never sent TEON (0x35). A 60 Hz panel
# pulses at least once per poll, so it always reads as pending; it is
# left off the interrupt line.
IRQ_TE = 1 << 1
# INTC line: its handler (kern 0x4021fe8) masks bit 22 and reads IRQ_STATUS;
# the UI waits on "LCD ASYNC OPERATION SEMAPHORE" for it
LCDC_IRQ_LINE = 22
FMT_RGB565 = 5


def rgb888_to_565(c):
    return (c >> 8 & 0xf800) | (c >> 5 & 0x7e0) | (c >> 3 & 0x1f)


def blend565(src, dst, alpha):
    out = 0
    for shift, mask in ((11, 0x1f), (5, 0x3f), (0, 0x1f)):
        a, b = src >> shift & mask, dst >> shift & mask
        out |= ((a * alpha + b * (255 - alpha)) // 255) << shift
    return out


class Lcdc:
    def __init__(self, machine, spi, frames_dir, max_frames=300):
        self.m = machine
        self.spi = spi
        self.raw = 0
        self.frames_dir = frames_dir
        self.max_frames = max_frames
        self.refreshes = 0
        self.saved = 0
        self.last_frame = None
        self.warned = set()

    def reg(self, off):
        return self.m.regs.get(LCDC_BASE + off, 0)

    def read_raw(self):
        return self.raw | IRQ_TE

    def read_status(self):
        return self.raw & self.reg(IRQ_EN)

    def on_clr(self, value):
        self.raw &= ~value
        self.m.regs[LCDC_BASE + IRQ_CLR] = 0
        self.update_line()

    def update_line(self):
        self.m.intc.set_line(LCDC_IRQ_LINE, self.raw & self.reg(IRQ_EN))

    def on_ctrl(self, value):
        if value & CTRL_REFRESH:
            self.m.regs[LCDC_BASE + LCDC_CTRL] = value & ~CTRL_REFRESH
            self.refresh()

    def warn(self, what):
        if what not in self.warned:
            self.warned.add(what)
            self.spi.panel.log(f"lcdc: {what}")

    def refresh(self):
        size = self.reg(LCDC_LCM_SIZE)
        w, h = size & 0xfff, size >> 16 & 0xfff
        frame = array("H", [rgb888_to_565(self.reg(LCDC_BG_COLOR))]) * (w * h)
        ctrl = self.reg(IMG_CTRL)
        if ctrl & 1:
            fmt = ctrl >> 4 & 0xf
            if fmt != FMT_RGB565 or (ctrl >> 8 & 3) != 2:
                self.warn(f"image layer format {fmt} endian {ctrl >> 8 & 3} not handled")
            else:
                self.blend_rgb565(frame, w, h, self.reg(IMG_BASE) << 2, self.reg(IMG_SIZE),
                                  self.reg(IMG_PITCH), self.reg(IMG_DISP_XY))
        for base in OSD_BASES:
            ctrl = self.reg(base + OSD_CTRL)
            if not ctrl & LAYER_ENABLE:
                continue
            fmt = ctrl >> 4 & 0xf
            if fmt != FMT_RGB565 or (ctrl >> 8 & 3) != 2:
                self.warn(f"OSD layer +{base:#x} format {fmt} endian {ctrl >> 8 & 3} not handled")
                continue
            if ctrl & LAYER_COLOR_KEY:
                self.warn(f"OSD layer +{base:#x} colour key not handled")
            alpha = self.reg(base + OSD_ALPHA) & 0xff if ctrl & LAYER_BLOCK_ALPHA else 0xff
            self.blend_rgb565(frame, w, h, self.reg(base + OSD_ADDR) << 2, self.reg(base + OSD_SIZE),
                              self.reg(base + OSD_PITCH), self.reg(base + OSD_DISP_XY), alpha)
        if (self.reg(CAP_BASE) << 2) == SPI1_BASE + TXD:
            self.spi.panel.stream(frame)
        else:
            self.warn(f"capture to {self.reg(CAP_BASE) << 2:#x} not handled")
        self.raw |= IRQ_REFRESH_DONE
        self.refreshes += 1
        self.save_frame()
        self.update_line()

    def blend_rgb565(self, frame, w, h, base, size, pitch, disp_xy, alpha=0xff):
        sw, sh = size & 0xfff, size >> 16 & 0xfff
        dx, dy = disp_xy & 0xfff, disp_xy >> 16 & 0xfff
        for row in range(min(sh, h - dy)):
            n = min(sw, w - dx)
            try:
                data = self.m.uc.mem_read(base + 2 * pitch * row, 2 * n)
            except Exception:
                self.warn(f"layer row at {base + 2 * pitch * row:#x} unreadable")
                return
            src = array("H", bytes(data))
            at = (dy + row) * w + dx
            if alpha >= 0xff:
                frame[at:at + n] = src
            elif alpha:
                frame[at:at + n] = array("H", (blend565(s, d, alpha) for s, d in zip(src, frame[at:at + n])))

    def save_frame(self):
        fb = self.spi.panel.fb.tobytes()
        if fb == self.last_frame:
            return
        self.last_frame = fb
        if self.saved < self.max_frames:
            self.saved += 1
            self.frames_dir.mkdir(exist_ok=True)
            self.spi.panel.save_png(self.frames_dir / f"frame_{self.saved:04d}.png")
            print(f"[block {self.m.nblocks}] lcd: new frame {self.saved} (refresh {self.refreshes})")
