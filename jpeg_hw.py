"""SC6531E hardware JPEG decoder (0x20200000 and 0x20210000 blocks).

The firmware parses the JPEG headers itself and hands the hardware only
tables and the entropy-coded scan (user 0x80fe800.., 0x8105ac0..):
  0x2020000c  image size: width bits 0-11, height bits 16-27
  0x20200040  output Y plane          0x20200044  output chroma
  0x20200048  second output Y         0x2020004c  second output chroma
  0x20200050  scan data (entropy-coded segment from the file)
  0x20200300  quantization tables: two tables of 32 words, each word two
              16-bit entries; entry i holds natural-order coefficient
              bitrev6(i) (user 0x8105b02 builds them through the 6-bit
              bit-reversal table at 0x82cb126); table 0 luma, 1 chroma
  0x20202000  AC Huffman symbols: byte 0 luma, byte 1 chroma (standard
              JPEG Annex K tables on this phone)
  0x20210400  scan length in words; writing bit 31 starts the decode
  0x20210418  bit 31 = done (polled by user 0x8110b20)
  0x2021087c  MCU count, which gives the chroma subsampling
  0x20200020  interrupt status, 0x20200024 enable, 0x20200028 write-1 clear;
              INTC line 38 (handler kern 0x40677e4: bits 0x460 are errors,
              bit 4, the only one with a callback (kern 0x4059354), is the
              end of a colour conversion, see Converter below)
The decode itself is done by rebuilding a baseline JPEG file from that
state and handing it to Pillow.

The same block also converts YCbCr to RGB565 and scales (the "convert"
mode, set up by kern 0x4059xxx after the decode's band loop):
  0x20200000  ctrl: bit 1 set (0x82) selects this mode
  0x20200040  input Y           0x20200044  input interleaved CbCr
  0x2020000c  input size        0x20200134/138  source offset / size
  0x20200010  output size       0x20200008  output format (6 = RGB565)
  0x20200050  output address
  0x20200064  bits 0 and 3 set (0x14001 then 0x14009) start it
The MMI then waits for interrupt bit 4. Without this mode it times out
after 3 s and converts in software (user 0x80fe308 -> kern 0x407a94c).
"""
import io

from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

SIZE = 0x2020000c
OUT_Y, OUT_C = 0x20200040, 0x20200044
SCAN = 0x20200050
QTAB = 0x20200300
AC_SYMBOLS = 0x20202000
START = 0x20210400
MCU_COUNT = 0x2021087c

ZIGZAG = [  # zigzag index -> natural (row-major) index
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63]

# JPEG Annex K tables: (BITS, HUFFVAL)
STD_DC_LUMA = ([0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0], list(range(12)))
STD_DC_CHROMA = ([0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0], list(range(12)))
STD_AC_LUMA = ([0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7d], bytes.fromhex(
    "01020300041105122131410613516107227114328191a1082342b1c11552d1f0"
    "2433627282090a161718191a25262728292a3435363738393a434445464748494a"
    "535455565758595a636465666768696a737475767778797a838485868788898a"
    "92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7"
    "c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9fa"))
STD_AC_CHROMA = ([0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77], bytes.fromhex(
    "000102031104052131061241510761711322328108144291a1b1c109233352f0"
    "156272d10a162434e125f11718191a262728292a35363738393a434445464748"
    "494a535455565758595a636465666768696a737475767778797a828384858687"
    "88898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3"
    "c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9fa"))


def bitrev6(i):
    return int(f"{i:06b}"[::-1], 2)


def quant_table(regs, table):
    """Natural-order quantization table from the hardware registers."""
    entries = []
    for w in range(32):
        word = regs.get(QTAB + 0x80 * table + 4 * w, 0)
        entries += [word & 0xffff, word >> 16]
    return [entries[bitrev6(k)] for k in range(64)]


def ac_symbols(regs, chroma):
    out = []
    for i in range(len(STD_AC_CHROMA[1] if chroma else STD_AC_LUMA[1])):
        out.append(regs.get(AC_SYMBOLS + 4 * i, 0) >> (8 if chroma else 0) & 0xff)
    return bytes(out)


def sampling(width, height, mcus):
    """Luma (H, V) factors whose MCU grid matches the programmed MCU count."""
    for h, v in ((2, 1), (2, 2), (1, 1)):
        if -(-width // (8 * h)) * -(-height // (8 * v)) == mcus:
            return h, v
    return None


def build_jpeg(width, height, hv, qtabs, scan):
    def seg(marker, body):
        return bytes([0xff, marker]) + (len(body) + 2).to_bytes(2, "big") + body
    out = b"\xff\xd8"
    for t, q in enumerate(qtabs):
        out += seg(0xdb, bytes([t]) + bytes(min(255, q[ZIGZAG[z]]) for z in range(64)))
    h, v = hv
    out += seg(0xc0, bytes([8]) + height.to_bytes(2, "big") + width.to_bytes(2, "big") +
               bytes([3, 1, h << 4 | v, 0, 2, 0x11, 1, 3, 0x11, 1]))
    for cls_id, (bits, vals) in ((0x00, STD_DC_LUMA), (0x01, STD_DC_CHROMA),
                                 (0x10, STD_AC_LUMA), (0x11, STD_AC_CHROMA)):
        out += seg(0xc4, bytes([cls_id]) + bytes(bits) + bytes(vals))
    out += seg(0xda, bytes([3, 1, 0x00, 2, 0x11, 3, 0x11, 0, 63, 0]))
    return out + scan + b"\xff\xd9"


def decode(regs, scan):
    """Returns (Y plane, chroma plane as interleaved CbCr, info) or raises."""
    size = regs.get(SIZE, 0)
    width, height = size & 0xfff, size >> 16 & 0xfff
    hv = sampling(width, height, regs.get(MCU_COUNT, 0) & 0x3ffff)
    if hv is None:
        raise ValueError(f"no sampling matches {width}x{height} and {regs.get(MCU_COUNT, 0)} MCUs")
    qtabs = [quant_table(regs, 0), quant_table(regs, 1)]
    standard = (ac_symbols(regs, False) == STD_AC_LUMA[1] and ac_symbols(regs, True) == STD_AC_CHROMA[1])
    img = Image.open(io.BytesIO(build_jpeg(width, height, hv, qtabs, scan)))
    img.draft("YCbCr", img.size)
    y, cb, cr = img.convert("YCbCr").split()
    cw, ch = width // hv[0], height // hv[1]
    cb = cb.resize((cw, ch), Image.BOX).tobytes()
    cr = cr.resize((cw, ch), Image.BOX).tobytes()
    chroma = bytes(b for pair in zip(cb, cr) for b in pair)
    info = f"{width}x{height} sampling {hv} standard-huffman {standard}"
    return y.tobytes(), chroma, info, img.convert("RGB")


# Band interface (user 0x80feb6a..0x80fec86): per band the driver selects
# an output pair through BAND_SEL bits 0-1, writes the band's MCU count
# to BAND_MCUS, starts it with BAND_START = 1, then polls PROGRESS
# ((MCU row << 16) | MCU column) and calls its converter on the band.
# The select is one-hot: 1 = (OUT_Y, OUT_C), 2 = (OUT_Y2, OUT_C2); the
# driver flips both bits each band while passing the converter a pair
# index that alternates 0, 1. BAND_SEL bit 2 reads 1 once the last band
# is done.
BAND_MCUS = 0x2021180c
BAND_SEL = 0x20211810
BAND_START = 0x20211818
PROGRESS = 0x20211808
OUT_Y2, OUT_C2 = 0x20200048, 0x2020004c
SEL_DONE = 1 << 2
INT_STS, INT_EN, INT_CLR = 0x20200020, 0x20200024, 0x20200028
INT_DONE = 1 << 4
IRQ_LINE = 38
CTRL = 0x20200000
CTRL_CONVERT = 1 << 1
OUT_FMT, OUT_SIZE = 0x20200008, 0x20200010
SRC_XY, SRC_SIZE = 0x20200134, 0x20200138
CONV_START = 0x20200064
FMT_RGB565 = 6


class JpegDecoder:
    def __init__(self, machine, log):
        self.m = machine
        self.log = log
        self.image = None       # (Y, Cb, Cr, width, height, (h, v))
        self.pos = 0            # MCUs decoded so far
        self.done = False
        self.decodes = 0
        self.int_raw = 0
        self.converts = 0

    def reg(self, a):
        return self.m.regs.get(a, 0)

    def on_start(self, value):
        if not value & 0x80000000:
            return
        scan = bytes(self.m.uc.mem_read(self.reg(SCAN), (value & 0xffff) * 4))
        self.pos, self.done = 0, False
        try:
            y, _, info, rgb = decode(self.m.regs, scan)
        except Exception as e:      # keep the guest running; report the failure
            self.image = None
            self.log(f"decode failed: {e}")
            return
        ycc = rgb.convert("YCbCr")
        size = self.reg(SIZE)
        width, height = size & 0xfff, size >> 16 & 0xfff
        hv = sampling(width, height, self.reg(MCU_COUNT) & 0x3ffff)
        _, cb, cr = ycc.split()
        cw, ch = width // hv[0], height // hv[1]
        self.image = (y, cb.resize((cw, ch), Image.BOX).tobytes(),
                      cr.resize((cw, ch), Image.BOX).tobytes(), width, height, hv)
        self.decodes += 1
        self.last_rgb = rgb
        self.log(f"decode {self.decodes}: {info}, scan {len(scan)} bytes")

    def on_band_start(self, value):
        if not value & 1 or self.image is None:
            return
        y, cb, cr, width, height, (h, v) = self.image
        per_row = -(-width // (8 * h))
        mcus = self.reg(BAND_MCUS) & 0xffff
        sel = self.reg(BAND_SEL)
        first_row, rows = self.pos // per_row, mcus // per_row
        py0, py1 = first_row * 8 * v, min(height, (first_row + rows) * 8 * v)
        y_out = self.reg(OUT_Y if sel & 1 else OUT_Y2)
        c_out = self.reg(OUT_C if sel & 1 else OUT_C2)
        self.m.uc.mem_write(y_out, y[py0 * width:py1 * width])
        cw = width // h
        cy0, cy1 = py0 // v, py1 // v
        chroma = bytearray()
        for row in range(cy0, cy1):    # interleaved CbCr per chroma row (guess)
            for x in range(cw):
                chroma += bytes((cb[row * cw + x], cr[row * cw + x]))
        self.m.uc.mem_write(c_out, bytes(chroma))
        self.pos += mcus
        self.m.regs[PROGRESS] = (self.pos // per_row) << 16 | (self.pos % per_row)
        self.done = self.pos >= per_row * -(-height // (8 * v))
        self.log(f"band rows {py0}-{py1} -> Y {y_out:08x} C {c_out:08x}, sel {sel & 3}, "
                 f"progress {self.pos} MCUs{' (done)' if self.done else ''}")

    def on_conv_start(self, value):
        if value & 9 != 9 or not self.reg(CTRL) & CTRL_CONVERT:
            return
        size, out_size = self.reg(SIZE), self.reg(OUT_SIZE) or self.reg(SIZE)
        w, h = size & 0xfff, size >> 16 & 0xfff
        ow, oh = out_size & 0xfff, out_size >> 16 & 0xfff
        if self.reg(OUT_FMT) != FMT_RGB565:
            self.log(f"convert: output format {self.reg(OUT_FMT)} not handled")
        # chroma rows follow the last decode's vertical subsampling
        v = self.image[5][1] if self.image else 1
        y = Image.frombytes("L", (w, h), bytes(self.m.uc.mem_read(self.reg(OUT_Y), w * h)))
        c = bytes(self.m.uc.mem_read(self.reg(OUT_C), w * (h // v)))
        cb = Image.frombytes("L", (w // 2, h // v), c[0::2]).resize((w, h))
        cr = Image.frombytes("L", (w // 2, h // v), c[1::2]).resize((w, h))
        rgb = Image.merge("YCbCr", (y, cb, cr)).convert("RGB")
        sx, sy = self.reg(SRC_XY) & 0xfff, self.reg(SRC_XY) >> 16 & 0xfff
        src = self.reg(SRC_SIZE)
        sw, sh = (src & 0xfff, src >> 16 & 0xfff) if src else (w, h)
        rgb = rgb.crop((sx, sy, sx + sw, sy + sh))
        if (sw, sh) != (ow, oh):
            rgb = rgb.resize((ow, oh), Image.BILINEAR)
        px = rgb.tobytes()
        out = bytearray(2 * ow * oh)
        for i in range(ow * oh):
            r, g, b = px[3 * i], px[3 * i + 1], px[3 * i + 2]
            v565 = (r >> 3) << 11 | (g >> 2) << 5 | b >> 3
            out[2 * i], out[2 * i + 1] = v565 & 0xff, v565 >> 8
        self.m.uc.mem_write(self.reg(SCAN), bytes(out))
        self.converts += 1
        self.log(f"convert {self.converts}: {w}x{h} src {sw}x{sh}+{sx}+{sy} -> {ow}x{oh} "
                 f"RGB565 at {self.reg(SCAN):08x}")
        self.int_raw |= INT_DONE
        self.update_line()

    def on_int_clr(self, value):
        self.int_raw &= ~value
        self.m.regs[INT_CLR] = 0
        self.update_line()

    def update_line(self):
        self.m.intc.set_line(IRQ_LINE, self.int_raw & self.reg(INT_EN))

    def read_sel(self):
        return self.reg(BAND_SEL) & ~SEL_DONE | (SEL_DONE if self.done else 0)
