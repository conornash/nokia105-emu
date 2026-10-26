#!/usr/bin/env python3
"""Live window for the emulator: shows the LCD and turns keyboard keys into
phone keys. While the phone sleeps, emulated time follows the wall clock;
while it is busy it runs slower than real time.

usage: .venv/bin/python -I live.py [--scale N] [emu.py options]

Keys: arrows = navigate, Enter/space = select, [ and ] (or F1/F2) = left
and right soft keys, Backspace = right soft key, c = call (green),
Escape = end/power (red), 0-9 * # = keypad, h = plug/unplug a headset.
"""
import argparse
import sys
import threading
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageTk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import emu  # noqa: E402
import lcd  # noqa: E402

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


class Window:
    def __init__(self, machine, scale):
        self.m = machine
        self.scale = scale
        self.root = tk.Tk()
        self.root.title("Nokia 105 (emulated)")
        self.screen = tk.Label(self.root, bg="black")
        self.screen.pack()
        self.status = tk.Label(self.root, anchor="w", font=("Menlo", 11))
        self.status.pack(fill="x")
        tk.Label(self.root, text=HELP, anchor="w", font=("Menlo", 10), fg="gray40").pack(fill="x")
        self.held = set()
        self.pending_release = {}
        self.shown = None
        self.root.bind("<KeyPress>", self.on_press)
        self.root.bind("<KeyRelease>", self.on_release)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
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
        if name not in self.held:
            self.held.add(name)
            self.m.inject("press", name)

    def on_release(self, event):
        name = KEYS.get(event.keysym)
        if name in self.held:
            self.pending_release[name] = self.root.after(REPEAT_MS, self.release, name)

    def release(self, name):
        self.pending_release.pop(name, None)
        self.held.discard(name)
        self.m.inject("release", name)

    def refresh(self):
        version = (self.m.lcdc.refreshes, self.m.panel.pixels)
        if version != self.shown:
            self.shown = version
            img = Image.frombuffer("RGB", (lcd.WIDTH, lcd.HEIGHT), self.m.panel.fb.tobytes(),
                                   "raw", "BGR;16", 0, 1)
            img = img.resize((lcd.WIDTH * self.scale, lcd.HEIGHT * self.scale), Image.NEAREST)
            self.photo = ImageTk.PhotoImage(img)
            self.screen.configure(image=self.photo)
        ms = self.m.nblocks // emu.BLOCKS_PER_MS
        state = self.m.stop_reason or ("booting" if self.m.lcdc.refreshes < 3 else "running")
        self.status.configure(text=f" {ms // 60000}:{ms // 1000 % 60:02d}.{ms // 100 % 10} emulated  {state}")
        if self.thread.is_alive():
            self.root.after(40, self.refresh)

    def close(self):
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
    p.add_argument("--scale", type=int, default=3, help="window pixels per LCD pixel")
    args = emu.parse_args(parser=p)
    # a live session has no block budget
    if "--max-blocks" not in sys.argv:
        args.max_blocks = 1 << 62
    machine = emu.Machine(args)
    machine.realtime = True
    Window(machine, args.scale).run()


if __name__ == "__main__":
    sys.exit(main())
