#!/usr/bin/env python3
"""Live window for the emulator: the LCD inside a drawing of the phone.
Clicks on the drawn keys and keyboard keys become phone key presses. While
the phone sleeps, emulated time follows the wall clock; while it is busy it
runs slower than real time.

usage: .venv/bin/python -I live.py [--scale N] [--no-skin] [emu.py options]

Keys: arrows = navigate, Enter/space = select, [ and ] (or F1/F2) = left
and right soft keys, Backspace = right soft key, c = call (green),
Escape = end/power (red), 0-9 * # = keypad, h = plug/unplug a headset,
s = screenshot, r = start/stop recording.

The screen dims and darkens with the phone's backlight, the key labels
glow while the keypad backlight is on, and the torch lights up on the top
edge (up twice on the home screen toggles it).

Holding the red key switches the phone off (Switch off!); holding it again
switches it back on, booting from the saved phone with its clock carried
on.

Screenshots (the screen at 3x, and the whole phone) and recordings (an
animated GIF of the screen) go to out/shots/.

Sound plays through ffplay (from ffmpeg) and is also saved to out/audio.wav.
Settings changed on the phone are kept in state/flash.bin between sessions:
saved 2 s after each change and when the window closes (close button,
Cmd+Q or Ctrl+C). --fresh starts again from the dump.
"""
import argparse
import datetime
import math
import signal
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageTk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import emu  # noqa: E402
import lcd  # noqa: E402
import lights  # noqa: E402
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
HELP = "arrows  Enter=select  [ ]=soft keys  c=call  Esc=end  0-9 * #  h=headset  s=screenshot  r=record"
# a key release this soon after its press is taken as autorepeat
REPEAT_MS = 40
# window height to leave for the title bar, the desktop's bars and the
# status lines when choosing a scale
ROOM = 120
# screenshots and recordings are scaled up by these, in whole pixels
SHOT_SCALE, GIF_SCALE = 3, 2
MAX_RECORDING_S = 120
# how bright the screen looks with the backlight off (a dark LCD still shows
# a faint image), at the lowest level and, scaled up to 1, at the highest
BACKLIGHT_OFF, BACKLIGHT_MIN = 0.08, 0.7


def brightness(level):
    if level is None:
        return BACKLIGHT_OFF
    return BACKLIGHT_MIN + (1 - BACKLIGHT_MIN) * min(level, lights.MAX_LEVEL) / lights.MAX_LEVEL


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
            # the phone with the keypad backlight off and on
            self.bodies = [ImageTk.PhotoImage(self.phone.image),
                           ImageTk.PhotoImage(self.phone.image_glowing)]
            self.body = self.canvas.create_image(0, 0, image=self.bodies[0], anchor="nw")
            at, img = self.phone.torch
            self.photos = [ImageTk.PhotoImage(img)]
            self.torch = self.canvas.create_image(*at, image=self.photos[0], anchor="nw", state="hidden")
            for name, (at, img) in self.phone.lit.items():
                self.photos.append(ImageTk.PhotoImage(img))
                self.lit[name] = self.canvas.create_image(*at, image=self.photos[-1], anchor="nw",
                                                          state="hidden")
            self.canvas.bind("<ButtonPress-1>", self.on_click)
            self.canvas.bind("<ButtonRelease-1>", self.on_unclick)
            self.canvas.bind("<Motion>", self.on_motion)
        self.screen = self.canvas.create_image(x, y, anchor="nw")
        bar = tk.Frame(self.root, bg=skin.BG)
        bar.pack(fill="x")
        self.status = tk.Label(bar, anchor="w", font=("Menlo", 11), bg=skin.BG)
        self.status.pack(side="left", fill="x", expand=True)
        self.record_button = tk.Button(bar, text="Record", command=self.toggle_recording,
                                       highlightbackground=skin.BG)
        self.record_button.pack(side="right")
        tk.Button(bar, text="Screenshot", command=self.screenshot,
                  highlightbackground=skin.BG).pack(side="right")
        tk.Label(self.root, text=HELP, anchor="w", justify="left", font=("Menlo", 10), fg="gray40",
                 bg=skin.BG, wraplength=width).pack(fill="x")
        self.held = set()
        self.pending_release = {}
        self.clicked = None
        self.shown = None
        self.lights = None
        self.lcd_image = None   # the screen as last drawn, at LCD size
        self.screen_image = None    # and as shown in the window
        self.recording = None   # [(wall time, screen)] while recording
        self.notice = None      # (text, until wall time) for the status line
        self.root.bind("<KeyPress>", self.on_press)
        self.root.bind("<KeyRelease>", self.on_release)
        # quitting by any route goes through close(), which saves the phone
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.createcommand("tk::mac::Quit", self.close)      # Cmd+Q on macOS
        for sig in (signal.SIGINT, signal.SIGTERM):                # Ctrl+C, kill
            signal.signal(sig, lambda *_: self.root.after(0, self.close))
        self.closing = False
        self.off_since = None   # wall time the phone switched itself off
        self.thread = threading.Thread(target=machine.run, daemon=True)

    def on_press(self, event):
        if event.keysym == "h":
            self.m.inject("headset", not self.m.eic.headset)
            return
        if event.keysym in ("s", "r"):
            (self.screenshot if event.keysym == "s" else self.toggle_recording)()
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
            if self.m.stop_reason == emu.POWERED_OFF and name == emu.keypad.POWER_KEY:
                self.power_on()
            else:
                self.m.inject("press", name)
            self.light(name, "normal")

    def power_on(self):
        """Switch the phone on again. A new machine boots from the saved
        phone with its clock carried on from the old one's, and the red key
        held for as long as it is held here: let go too soon and the
        firmware switches off again, as on the phone."""
        self.thread.join()          # the old machine saves the phone as it ends
        old = self.m
        off_for = time.monotonic() - (self.off_since or time.monotonic())
        old.args.rtc_time = old.pmic.clock() + datetime.timedelta(seconds=off_for)
        old.args.power_key_ms = 0
        machine = emu.Machine(old.args)
        machine.realtime = True
        machine.vbc.live = old.vbc.live
        machine.keypad.press(emu.keypad.POWER_KEY)
        self.m, self.off_since = machine, None
        self.thread = threading.Thread(target=machine.run, daemon=True)
        self.thread.start()

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
        backlight, keypad, torch = self.m.lights.state
        if self.phone and (keypad, torch) != self.lights:
            self.canvas.itemconfigure(self.body, image=self.bodies[keypad])
            self.canvas.itemconfigure(self.torch, state="normal" if torch else "hidden")
        self.lights = keypad, torch
        off = self.m.stop_reason == emu.POWERED_OFF
        if off and self.off_since is None:
            self.off_since = time.monotonic()
        version = (self.m.lcdc.refreshes, self.m.panel.pixels, backlight, off)
        if version != self.shown:
            if self.shown is None or version[:2] != self.shown[:2]:
                self.lcd_image = Image.frombuffer("RGB", (lcd.WIDTH, lcd.HEIGHT), self.m.panel.fb.tobytes(),
                                                  "raw", "BGR;16", 0, 1)
                if self.recording is not None:
                    self.recording.append((time.monotonic(), self.lcd_image))
            self.shown = version
            img = self.lcd_image
            shade = 0 if off else brightness(backlight)
            if shade < 1:
                img = img.point(lambda v: round(v * shade))
            # whole pixels first, then averaged down to a fractional scale
            n = math.ceil(self.lcd_size[0] / lcd.WIDTH)
            img = img.resize((lcd.WIDTH * n, lcd.HEIGHT * n), Image.NEAREST)
            if img.size != self.lcd_size:
                img = img.resize(self.lcd_size, Image.BOX)
            self.screen_image = img
            self.photo = ImageTk.PhotoImage(img)
            self.canvas.itemconfigure(self.screen, image=self.photo)
        ms = self.m.nblocks // emu.BLOCKS_PER_MS
        if off:
            state = "switched off: hold the red key (Esc) to switch on"
        else:
            state = self.m.stop_reason or ("booting" if self.m.lcdc.refreshes < 3 else "running")
        notes = ["torch on"] if torch else []
        if self.recording:
            seconds = time.monotonic() - self.recording[0][0]
            if seconds >= MAX_RECORDING_S:
                self.toggle_recording()
            else:
                notes.append(f"recording {int(seconds) // 60}:{int(seconds) % 60:02d}")
        if self.notice and time.monotonic() < self.notice[1]:
            notes.append(self.notice[0])
        self.status.configure(text=f" {ms // 60000}:{ms // 1000 % 60:02d}.{ms // 100 % 10} emulated  "
                                   + "  ".join([state] + notes))
        if not self.closing:
            self.root.after(40, self.refresh)

    def shot_path(self, suffix):
        folder = self.m.args.out / "shots"
        folder.mkdir(parents=True, exist_ok=True)
        stem = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path, n = folder / (stem + suffix), 2
        while path.exists():
            path, n = folder / f"{stem}-{n}{suffix}", n + 1
        return path

    def saved(self, path):
        self.notice = (f"saved {path.name}", time.monotonic() + 4)
        print(f"saved {path}", flush=True)

    def screenshot(self):
        """The screen as the phone drew it, scaled up, and when the phone is
        drawn, the whole window's phone as it looks now."""
        if self.lcd_image is None:
            return
        path = self.shot_path(".png")
        self.lcd_image.resize((lcd.WIDTH * SHOT_SCALE, lcd.HEIGHT * SHOT_SCALE), Image.NEAREST).save(path)
        if self.phone:
            keypad, torch = self.lights
            img = (self.phone.image_glowing if keypad else self.phone.image).copy()
            for name in self.held:
                if name in self.phone.lit:
                    at, over = self.phone.lit[name]
                    img.paste(over, at, over)
            if torch:
                at, over = self.phone.torch
                img.paste(over, at, over)
            img.paste(self.screen_image, self.phone.lcd_box[:2])
            img.save(path.with_name(path.stem + "-phone.png"))
        self.saved(path)

    def toggle_recording(self):
        if self.recording is None:
            if self.lcd_image is not None:
                self.recording = [(time.monotonic(), self.lcd_image)]
                self.record_button.configure(text="Stop")
            return
        frames, self.recording = self.recording, None
        self.record_button.configure(text="Record")
        times = [t for t, _ in frames] + [time.monotonic()]
        durations = [max(20, round((b - a) * 1000)) for a, b in zip(times, times[1:])]
        size = (lcd.WIDTH * GIF_SCALE, lcd.HEIGHT * GIF_SCALE)
        images = [img.resize(size, Image.NEAREST) for _, img in frames]
        path = self.shot_path(".gif")
        images[0].save(path, save_all=True, append_images=images[1:], duration=durations, loop=0)
        self.saved(path)

    def close(self):
        if self.closing:
            return
        self.closing = True
        if self.recording is not None:
            self.toggle_recording()
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
