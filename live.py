#!/usr/bin/env python3
"""Live window for the emulator: the LCD inside a drawing of the phone.
Clicks on the drawn keys and keyboard keys become phone key presses. While
the phone sleeps, emulated time follows the wall clock; while it is busy it
runs slower than real time.

usage: .venv/bin/python -I live.py [--scale N] [--no-skin] [emu.py options]

Keys: arrows = navigate, Enter/space = select, [ and ] (or F1/F2) = left
and right soft keys, Backspace = right soft key, c = call (green),
Escape = end/power (red), 0-9 * # = keypad, h = plug/unplug a headset.

Sound plays through ffplay (from ffmpeg) and is also saved to out/audio.wav.
Settings changed on the phone are kept in state/flash.bin between sessions:
saved 2 s after each change and when the window closes (close button,
Cmd+Q or Ctrl+C). --fresh starts again from the dump.
"""
import argparse
import math
import signal
import sys
import threading
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageTk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import emu  # noqa: E402
import lcd  # noqa: E402
import skin  # noqa: E402

KEYS = {
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "Return": "select", "KP_Enter": "select", "space": "select",
    "bracketleft": "lsoft", "F1": "lsoft",
    "bracketright": "rsoft", "F2": "rsoft", "BackSpace": "rsoft",
    "c": "call", "Escape": "end",
    "asterisk": "*", "numbersign": "#",
    **{str(d): str(d) for d in range(10)},
    **{f"KP_{d}": str(d) for d in range(10)},
}
HELP = "arrows  Enter=select  [ ]=soft keys  c=call  Esc=end  0-9 * #  h=headset"
# a key release this soon after its press is taken as autorepeat
REPEAT_MS = 40
# window height to leave for the title bar, the desktop's bars and the
# status lines when choosing a scale
ROOM = 120


class Window:
    def __init__(self, machine, scale, skinned):
        self.m = machine
        self.root = tk.Tk()
        self.root.title("Nokia 105 (emulated)")
        self.root.configure(bg=skin.BG)
        if scale is None:
            tall = skin.H + 2 * skin.MARGIN if skinned else lcd.HEIGHT
            scale = max(1, min(3, (self.root.winfo_screenheight() - ROOM) / tall // 0.25 * 0.25))
        self.scale = scale
        self.phone = skin.Phone(self.scale) if skinned else None
        if self.phone:
            width, height = self.phone.size
            x, y, x1, y1 = self.phone.lcd_box
            self.lcd_size = (x1 - x, y1 - y)
        else:
            x = y = 0
            self.lcd_size = width, height = (round(lcd.WIDTH * self.scale), round(lcd.HEIGHT * self.scale))
        self.canvas = tk.Canvas(self.root, width=width, height=height, bg="black",
                                highlightthickness=0, borderwidth=0)
        self.canvas.pack()
        self.lit = {}
        if self.phone:
            self.photos = [ImageTk.PhotoImage(self.phone.image)]
            self.canvas.create_image(0, 0, image=self.photos[0], anchor="nw")
            for name, (at, img) in self.phone.lit.items():
                self.photos.append(ImageTk.PhotoImage(img))
                self.lit[name] = self.canvas.create_image(*at, image=self.photos[-1], anchor="nw",
                                                          state="hidden")
            self.canvas.bind("<ButtonPress-1>", self.on_click)
            self.canvas.bind("<ButtonRelease-1>", self.on_unclick)
            self.canvas.bind("<Motion>", self.on_motion)
        self.screen = self.canvas.create_image(x, y, anchor="nw")
        self.status = tk.Label(self.root, anchor="w", font=("Menlo", 11), bg=skin.BG)
        self.status.pack(fill="x")
        tk.Label(self.root, text=HELP, anchor="w", justify="left", font=("Menlo", 10), fg="gray40",
                 bg=skin.BG, wraplength=width).pack(fill="x")
        self.held = set()
        self.pending_release = {}
        self.clicked = None
        self.shown = None
        self.root.bind("<KeyPress>", self.on_press)
        self.root.bind("<KeyRelease>", self.on_release)
        # quitting by any route goes through close(), which saves the phone
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.createcommand("tk::mac::Quit", self.close)      # Cmd+Q on macOS
        for sig in (signal.SIGINT, signal.SIGTERM):                # Ctrl+C, kill
            signal.signal(sig, lambda *_: self.root.after(0, self.close))
        self.closing = False
        self.thread = threading.Thread(target=machine.run, daemon=True)

    def on_press(self, event):
        if event.keysym == "h":
            self.m.inject("headset", not self.m.eic.headset)
            return
        name = KEYS.get(event.keysym)
        if name is None:
            return
        cancel = self.pending_release.pop(name, None)
        if cancel is not None:          # autorepeat: the key never went up
            self.root.after_cancel(cancel)
            return
        self.press(name)

    def on_release(self, event):
        name = KEYS.get(event.keysym)
        if name in self.held:
            self.pending_release[name] = self.root.after(REPEAT_MS, self.release, name)

    def on_click(self, event):
        self.clicked = self.phone.key_at(event.x, event.y)
        if self.clicked:
            self.press(self.clicked)

    def on_unclick(self, event):
        if self.clicked:
            self.release(self.clicked)
            self.clicked = None

    def on_motion(self, event):
        self.canvas.configure(cursor="hand2" if self.phone.key_at(event.x, event.y) else "")

    def press(self, name):
        if name not in self.held:
            self.held.add(name)
            self.m.inject("press", name)
            self.light(name, "normal")

    def release(self, name):
        self.pending_release.pop(name, None)
        if name in self.held:
            self.held.discard(name)
            self.m.inject("release", name)
            self.light(name, "hidden")

    def light(self, name, state):
        if name in self.lit:
            self.canvas.itemconfigure(self.lit[name], state=state)

    def refresh(self):
        version = (self.m.lcdc.refreshes, self.m.panel.pixels)
        if version != self.shown:
            self.shown = version
            img = Image.frombuffer("RGB", (lcd.WIDTH, lcd.HEIGHT), self.m.panel.fb.tobytes(),
                                   "raw", "BGR;16", 0, 1)
            # whole pixels first, then averaged down to a fractional scale
            n = math.ceil(self.lcd_size[0] / lcd.WIDTH)
            img = img.resize((lcd.WIDTH * n, lcd.HEIGHT * n), Image.NEAREST)
            if img.size != self.lcd_size:
                img = img.resize(self.lcd_size, Image.BOX)
            self.photo = ImageTk.PhotoImage(img)
            self.canvas.itemconfigure(self.screen, image=self.photo)
        ms = self.m.nblocks // emu.BLOCKS_PER_MS
        state = self.m.stop_reason or ("booting" if self.m.lcdc.refreshes < 3 else "running")
        self.status.configure(text=f" {ms // 60000}:{ms // 1000 % 60:02d}.{ms // 100 % 10} emulated  {state}")
        if self.thread.is_alive():
            self.root.after(40, self.refresh)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.m.stop("window closed")
        self.m.input_ready.set()
        self.thread.join(timeout=10)
        self.root.destroy()

    def run(self):
        self.thread.start()
        self.refresh()
        self.root.mainloop()


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--scale", type=float, default=None,
                   help="window pixels per LCD pixel (default: the largest up to 3 that fits the screen)")
    p.add_argument("--no-skin", dest="skin", action="store_false",
                   help="show only the screen, without the phone around it")
    p.add_argument("--mute", action="store_true", help="don't play sound (it is still saved to out/audio.wav)")
    p.add_argument("--fresh", action="store_true",
                   help="start from the dump, ignoring the saved phone state (it is replaced on exit)")
    args = emu.parse_args(parser=p)
    # a live session has no block budget
    if "--max-blocks" not in sys.argv:
        args.max_blocks = 1 << 62
    # the live phone keeps its flash between sessions
    if args.state is None:
        args.state = emu.ROOT / "state" / "flash.bin"
    if args.fresh and args.state.exists():
        args.state.rename(args.state.with_name(args.state.name + ".old"))
    machine = emu.Machine(args)
    machine.realtime = True
    machine.vbc.live = not args.mute
    Window(machine, args.scale, args.skin).run()


if __name__ == "__main__":
    sys.exit(main())
