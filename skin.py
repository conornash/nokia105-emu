"""The phone drawn around the LCD in the live window: the front of a Nokia
105 (2022), where every key is a region that can be clicked and lights up
while it is held, the key labels glow while the keypad backlight is on, and
the torch on the top edge lights up.

Lengths are in LCD pixels (the screen is 128x160 of them) and scaled to the
window. Everything is drawn SS times larger and scaled down for smooth
edges.
"""
import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

import lcd

W, H = 208, 490                 # phone body
MARGIN = 12                     # around the body, for its shadow
LCD_AT = (40, 48)               # top left of the screen
SS = 3

BG = "#e4e1dc"
RIM, BODY = "#4a4b50", "#17181b"
PANEL, PANEL_RIM = "#232427", "#393a3f"
KEY, KEY_TOP = "#36373c", "#4b4c52"
LIT, LIT_TOP = "#62656d", "#7a7d85"
INK = "#dadada"
UNLIT_INK = "#97989c"           # key labels with the keypad backlight off
HALO = "#ffffff"                # and the light around them when it is on
TORCH_AT = (W / 2, 2.5)         # the torch LED, on the top edge
TORCH_LIGHT = "#fff4d6"

# key columns; the row above the digits uses them too
COLS = (12, 76, 140)
KEY_W = 56
ROCKER = (264, 324)             # top and bottom of the two rocker keys
PAD = (76, 266, 132, 320)       # navigation key
SELECT = (85, 275, 123, 311)    # its centre
ROW_TOP, ROW_H, ROW_PITCH = 332, 32, 37
# digit and what is printed next to it; "oo", "_" and "^" are drawn as
# the voicemail, space and shift symbols
DIGITS = (("1", "oo"), ("2", "abc"), ("3", "def"),
          ("4", "ghi"), ("5", "jkl"), ("6", "mno"),
          ("7", "pqrs"), ("8", "tuv"), ("9", "wxyz"),
          ("*", "+"), ("0", "_"), ("#", "^"))
DIGIT_SIZE, LETTER_SIZE = 16, 10


def digit_box(i):
    x, y = COLS[i % 3], ROW_TOP + i // 3 * ROW_PITCH
    return x, y, x + KEY_W, y + ROW_H


def rocker(x0):
    """Outline of the rocker key in column x0, and the radius to round it
    by: its top slopes down towards the navigation key."""
    r = 9
    left, right = x0 + r, x0 + KEY_W - r
    top, bottom = ROCKER[0] + r, ROCKER[1] - r
    drop_left, drop_right = (0, 4) if x0 < PAD[0] else (4, 0)
    return [(left, top + drop_left), (right, top + drop_right), (right, bottom), (left, bottom)], r


def regions():
    """(phone key, shape) to click and light up, in drawing order: a later
    shape covers an earlier one. A shape is a polygon or (box, radius)."""
    mid = sum(ROCKER) / 2
    for x, top, bottom in ((COLS[0], "lsoft", "call"), (COLS[2], "rsoft", "end")):
        yield top, box_poly(x - 2, ROCKER[0] - 2, x + KEY_W + 2, mid)
        yield bottom, box_poly(x - 2, mid, x + KEY_W + 2, ROCKER[1] + 2)
    x0, y0, x1, y1 = grow(PAD, 2)
    c = ((x0 + x1) / 2, (y0 + y1) / 2)
    yield "up", [c, (x0, y0), (x1, y0)]
    yield "right", [c, (x1, y0), (x1, y1)]
    yield "down", [c, (x1, y1), (x0, y1)]
    yield "left", [c, (x0, y1), (x0, y0)]
    yield "select", (grow(SELECT, 1), 9)
    for i, (digit, _) in enumerate(DIGITS):
        yield digit, box_poly(*grow(digit_box(i), 2))


def grow(box, d):
    x0, y0, x1, y1 = box
    return x0 - d, y0 - d, x1 + d, y1 + d


def box_poly(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def bounds(shape):
    if isinstance(shape, list):
        xs, ys = zip(*shape)
        return min(xs), min(ys), max(xs), max(ys)
    return shape[0]


class Pen:
    """Draws on an image in LCD pixels, k image pixels to one."""

    def __init__(self, img, k, ink=INK):
        self.d = ImageDraw.Draw(img)
        self.k = k
        self.ink = ink          # colour of key labels and other printing
        self.fonts = {}

    def p(self, *v):
        """LCD-pixel coordinates (x, y, x, y, ...) to image pixels."""
        return [(MARGIN + c) * self.k for c in v]

    def pts(self, points):
        return [tuple(self.p(x, y)) for x, y in points]

    def w(self, width):
        return max(1, round(width * self.k))

    def rrect(self, box, r, fill=None, outline=None, width=1, corners=None):
        self.d.rounded_rectangle(self.p(*box), r * self.k, fill=fill, outline=outline,
                                 width=self.w(width), corners=corners)

    def blob(self, box, r_top, r_bottom, fill):
        """Rounded rectangle with different radii at the top and bottom."""
        x0, y0, x1, y1 = box
        mid = (y0 + y1) / 2
        self.rrect((x0, y0, x1, mid + 1), r_top, fill, corners=(True, True, False, False))
        self.rrect((x0, mid, x1, y1), r_bottom, fill, corners=(False, False, True, True))

    def roundpoly(self, points, r, fill):
        """A convex polygon grown by r on every side."""
        self.d.polygon(self.pts(points), fill=fill)
        self.d.line(self.pts(points + points[:1]), fill=fill, width=self.w(2 * r))
        for x, y in points:
            self.d.ellipse(self.p(x - r, y - r, x + r, y + r), fill=fill)

    def shape(self, shape, fill):
        if isinstance(shape, list):
            self.d.polygon(self.pts(shape), fill=fill)
        else:
            self.rrect(shape[0], shape[1], fill)

    def line(self, points, fill, width):
        self.d.line(self.pts(points), fill=fill, width=self.w(width), joint="curve")
        r = width / 2
        for x, y in (points[0], points[-1]):
            self.d.ellipse(self.p(x - r, y - r, x + r, y + r), fill=fill)

    def arc(self, box, start, end, fill, width):
        self.d.arc(self.p(*box), start, end, fill=fill, width=self.w(width))

    def ellipse(self, box, fill=None, outline=None, width=1):
        self.d.ellipse(self.p(*box), fill=fill, outline=outline, width=self.w(width))

    def font(self, size):
        if size not in self.fonts:
            self.fonts[size] = ImageFont.load_default(round(size * self.k))
        return self.fonts[size]

    def length(self, text, size):
        return self.font(size).getlength(text) / self.k

    def text(self, x, y, text, size, fill=None, anchor="ls", bold=0):
        fill = fill or self.ink
        self.d.text(self.p(x, y), text, font=self.font(size), fill=fill, anchor=anchor,
                    stroke_width=round(bold * self.k), stroke_fill=fill)


def draw_body(pen):
    pen.blob((0, 0, W, H), 40, 22, RIM)
    pen.blob((1.2, 1.2, W - 1.2, H - 1.2), 39, 21, BODY)
    pen.blob((5, 5, W - 5, 258), 35, 32, PANEL_RIM)
    pen.blob((6, 6, W - 6, 257), 34, 31, PANEL)
    # earpiece
    pen.rrect((W / 2 - 28, 23, W / 2 + 28, 29), 3, "#55565b")
    pen.rrect((W / 2 - 27, 24, W / 2 + 27, 28), 2, "#0c0c0d")
    # torch lens
    x, y = TORCH_AT
    pen.rrect((x - 5, y - 1, x + 5, y + 1), 1, "#6e6f74")
    # screen window
    x, y = LCD_AT
    pen.rrect((x - 3.5, y - 3.5, x + lcd.WIDTH + 3.5, y + lcd.HEIGHT + 3.5), 2, "#3d3e43")
    pen.rrect((x - 2.7, y - 2.7, x + lcd.WIDTH + 2.7, y + lcd.HEIGHT + 2.7), 1.5, "#000000")
    # logo, spaced out
    size, gap = 11, 1.6
    width = sum(pen.length(c, size) for c in "NOKIA") + gap * 4
    x = W / 2 - width / 2
    for c in "NOKIA":
        pen.text(x, 237, c, size, bold=0.35)
        x += pen.length(c, size) + gap


def draw_keys(pen, lit):
    fill, top = (LIT, LIT_TOP) if lit else (KEY, KEY_TOP)

    def key(shape_fn):
        # a lighter top edge, then the key face
        shape_fn(top, 0)
        shape_fn(fill, 0.8)

    for x0, icon in ((COLS[0], handset), (COLS[2], end_icon)):
        points, r = rocker(x0)
        key(lambda colour, dy: pen.roundpoly([(x, y + dy) for x, y in points], r - dy / 2, colour))
        cx = x0 + KEY_W / 2
        pen.line([(cx - 8.5, ROCKER[0] + 12), (cx + 8.5, ROCKER[0] + 12)], pen.ink, 1.3)
        icon(pen, cx, ROCKER[0] + 40)

    x0, y0, x1, y1 = PAD
    key(lambda colour, dy: pen.rrect((x0, y0 + dy, x1, y1), 12, colour))
    pen.rrect(SELECT, 8, LIT if lit else "#2c2d31", outline="#5a5b61", width=1)

    for i, (digit, extra) in enumerate(DIGITS):
        x0, y0, x1, y1 = digit_box(i)
        key(lambda colour, dy: pen.rrect((x0, y0 + dy, x1, y1), 8, colour))
        label(pen, i, digit, extra, (x0 + x1) / 2, (y0 + y1) / 2 + 6)
        if digit == "5":
            # the raised marks either side of the 5
            for x in (x0 + 5, x1 - 5):
                pen.line([(x, y0 + 12), (x, y1 - 10)], pen.ink, 0.9)


def label(pen, i, digit, extra, cx, base):
    """The digit and what goes with it, centred on cx; digits sit on the
    inside of the outer columns."""
    gap = 2.5
    if digit == "*":
        widths = (10, pen.length(extra, LETTER_SIZE))
    else:
        widths = (pen.length(digit, DIGIT_SIZE), extra_width(pen, extra))
    if i % 3 == 2:
        order = ((extra, widths[1]), (digit, widths[0]))
    else:
        order = ((digit, widths[0]), (extra, widths[1]))
    x = cx - (sum(widths) + gap) / 2
    for text, width in order:
        if text == digit:
            if digit == "*":
                asterisk(pen, x + 5, base - 5.5, 5)
            else:
                pen.text(x, base, digit, DIGIT_SIZE)
        else:
            extra_glyph(pen, text, x, base)
        x += width + gap


def extra_width(pen, extra):
    return {"oo": 8.5, "_": 7.5, "^": 7}.get(extra) or pen.length(extra, LETTER_SIZE)


def extra_glyph(pen, extra, x, base):
    if extra == "oo":           # voicemail
        for cx in (x + 2, x + 6.5):
            pen.ellipse((cx - 2, base - 5, cx + 2, base - 1), outline=pen.ink, width=0.9)
        pen.line([(x + 2, base - 1.4), (x + 6.5, base - 1.4)], pen.ink, 0.9)
    elif extra == "_":          # space
        pen.line([(x, base - 3), (x, base - 0.8), (x + 7.5, base - 0.8), (x + 7.5, base - 3)], pen.ink, 0.9)
    elif extra == "^":          # shift
        pen.d.polygon(pen.pts([(x + 3.5, base - 7.5), (x + 7, base - 4), (x + 5, base - 4),
                               (x + 5, base - 0.5), (x + 2, base - 0.5), (x + 2, base - 4),
                               (x, base - 4)]), outline=pen.ink, width=pen.w(0.8))
    else:
        pen.text(x, base, extra, LETTER_SIZE)


def asterisk(pen, cx, cy, r):
    for angle in (90, 30, 150):
        dx, dy = r * math.cos(math.radians(angle)), r * math.sin(math.radians(angle))
        pen.line([(cx - dx, cy - dy), (cx + dx, cy + dy)], pen.ink, 1.2)


def handset(pen, cx, cy):
    """The call symbol: a phone handset, opening downwards."""
    pen.arc((cx - 8, cy - 4, cx + 8, cy + 8), 200, 340, pen.ink, 1.4)
    for side in (-1, 1):
        x, y = cx + side * 7.5, cy + 1.8
        pen.line([(x, y), (x + side * 1.2, y + 2.6)], pen.ink, 2.2)


def end_icon(pen, cx, cy):
    handset(pen, cx, cy)
    pen.line([(cx - 8.5, cy + 4.6), (cx + 8.5, cy + 4.6)], pen.ink, 1.2)
    # a power symbol inside
    pen.arc((cx - 2.2, cy - 0.6, cx + 2.2, cy + 3.8), -50, 230, pen.ink, 0.8)
    pen.line([(cx, cy - 1.2), (cx, cy + 1.2)], pen.ink, 0.8)


class Phone:
    """The phone at `scale` window pixels per LCD pixel: its image with the
    keypad backlight off and on, where the screen goes, which key is under
    a point, each key lit up, and the torch's glow."""

    def __init__(self, scale):
        self.scale = scale
        self.size = (round((W + 2 * MARGIN) * scale), round((H + 2 * MARGIN) * scale))
        x, y = (round((MARGIN + v) * scale) for v in LCD_AT)
        self.lcd_box = (x, y, x + round(lcd.WIDTH * scale), y + round(lcd.HEIGHT * scale))

        big = (self.size[0] * SS, self.size[1] * SS)
        k = scale * SS
        body = Image.new("RGBA", big)
        draw_body(Pen(body, k))
        layers = []
        for lit, ink in ((False, UNLIT_INK), (False, INK), (True, INK)):
            img = body.copy()
            draw_keys(Pen(img, k, ink), lit)
            layers.append(img.resize(self.size, Image.BOX))
        phone, glowing, lit = layers
        # backlit labels bleed light around them
        halo = ImageChops.difference(glowing, phone).convert("L").point(lambda v: 255 if v else 0)
        halo = halo.filter(ImageFilter.GaussianBlur(1.5 * scale)).point(lambda v: v // 2)
        glowing.paste(HALO, mask=halo)
        lit.paste(HALO, mask=halo)

        # the shadow, from the body's outline
        shadow = Image.new("L", self.size)
        shadow.paste(phone.getchannel("A"), (0, round(3 * scale)))
        shadow = shadow.filter(ImageFilter.GaussianBlur(4 * scale)).point(lambda v: v * 2 // 5)
        ground = Image.new("RGBA", self.size, BG)
        ground.paste("#000000", mask=shadow)
        self.image = Image.alpha_composite(ground, phone).convert("RGB")
        self.image_glowing = Image.alpha_composite(ground, glowing).convert("RGB")
        lit = Image.alpha_composite(ground, lit).convert("RGB")

        # a map of which key is where, one shade per key
        keymap = Image.new("L", big)
        pen = Pen(keymap, k)
        self.names = [None]
        for name, shape in regions():
            self.names.append(name)
            pen.shape(shape, len(self.names) - 1)
        self.keymap = keymap.resize(self.size, Image.NEAREST)

        # each key lit up: an image of its region to lay over the phone
        self.lit = {}
        for i, (name, shape) in enumerate(regions(), 1):
            x0, y0, x1, y1 = bounds(shape)
            box = (math.floor((MARGIN + x0) * scale), math.floor((MARGIN + y0) * scale),
                   math.ceil((MARGIN + x1) * scale), math.ceil((MARGIN + y1) * scale))
            mask = keymap.crop([v * SS for v in box]).point(lambda v, i=i: 255 if v == i else 0)
            over = lit.crop(box).convert("RGBA")
            over.putalpha(mask.resize(over.size, Image.BOX))
            self.lit[name] = (box[:2], over)

        # the torch lit: a glow around the lens, cut off by the window's top
        cx, cy = ((MARGIN + v) * scale for v in TORCH_AT)
        rx, ry = round(48 * scale), round(30 * scale)
        falloff = Image.radial_gradient("L").resize((2 * rx, 2 * ry), Image.BILINEAR)
        glow = Image.new("RGBA", falloff.size, TORCH_LIGHT)
        # the gradient reads about 180 at the middle of each edge
        glow.putalpha(falloff.point(lambda v: round(255 * max(0, 1 - v / 180) ** 1.6)))
        top = round(cy) - ry
        self.torch = ((round(cx) - rx, max(0, top)), glow.crop((0, max(0, -top), *glow.size)))

    def key_at(self, x, y):
        """The phone key under window pixel x, y, or None."""
        if 0 <= x < self.size[0] and 0 <= y < self.size[1]:
            return self.names[self.keymap.getpixel((x, y))]
        return None
