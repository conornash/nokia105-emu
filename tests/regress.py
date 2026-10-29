#!/usr/bin/env python3
"""Regression check: runs scripted sessions headless and compares what the
phone did with a reference recorded earlier from the same dump.

usage: .venv/bin/python -I tests/regress.py [--update] [NAME ...]
       (make test, make test-update)

Each scenario boots the dump (never the saved phone in state/), presses
keys at fixed emulated times and records the log (key presses, new
screens, the RTC alarm, lights), every screen and the sound. Emulation is
deterministic, so a scenario has to reproduce its reference exactly; a
difference is reported with the first log line, screen and moment of sound
that changed, and the first changed screen is saved next to the reference
one in out/test/NAME/. A few checks that need no reference (the alarm
rings at 09:01, the torch lights up) run as well.

The reference lives in tests/reference/ and is not committed, because it
is made from your firmware. Record it with --update on a version you
trust, e.g. right after checking out a release; --update refuses to record
a run that fails the checks.
"""
import argparse
import concurrent.futures
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import wave
from array import array
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = ROOT / "tests" / "reference"
OUT = ROOT / "out" / "test"
BLOCKS_PER_MS = 4000
PEAK_MS = 100               # sound is compared as peaks over windows this long
LOUD, QUIET = 2000, 200     # peak levels for the sound checks

# Each scenario: RTC time at power-on, emulated length, keys (MS:KEY[:HOLD]),
# how the run ends if not by using up its time, and checks: (log line
# pattern, from ms, to ms) that must appear in that window, and
# ("loud" | "quiet", from ms, to ms) for the sound.
SCENARIOS = {
    "tour": dict(
        about="boot, then the main menu, Messages, Calendar and back home",
        rtc="2026-10-27T10:09:00", ms=40000,
        keys="10000:select,12500:select,15000:down,17500:rsoft,20000:down,22500:right,"
             "25000:select,28000:rsoft,30500:down,33000:end",
        checks=[("backlight on", 0, 2000), ("keypad light on", 0, 3000),
                # the dump is in the Silent profile
                ("quiet", 0, 40000)]),
    "day": dict(
        about="General profile, Alarm1 set for 09:01, backlight timeout, the alarm "
              "rung and stopped, the torch on and off",
        rtc="2026-10-29T08:59:35", ms=112000,
        keys="10000:select,11500:down,13000:down,14500:left,16000:select,17500:down,"
             "19000:down,20500:select,22500:up,24000:select,25500:select,28000:end,"
             "30000:rsoft,31500:select,33000:select,35000:right,36500:down,38000:lsoft,"
             "40000:select,43000:select,46000:end,100000:lsoft,105000:up,105300:up,108000:up",
        checks=[("keypad light off", 50000, 60000), ("backlight off", 50000, 60000),
                ("rtc: alarm 2026-10-29 09:01:00", 84900, 85100), ("backlight on", 85000, 86000),
                ("loud", 85500, 99500), ("quiet", 101000, 112000),
                ("torch on", 105000, 106000), ("torch off", 108000, 109000)]),
    "power": dict(
        about="holding the red key on the home screen switches the phone off",
        rtc="2026-10-29T09:00:05", ms=20000, keys="10000:end:4000", stop="powered off",
        checks=[("backlight off", 12000, 14000), ("power off", 13000, 15000)]),
}


class Run:
    """What one run of a scenario did, from its output directory or a
    reference: log lines, a hash per screen, and the sound."""

    def __init__(self, folder, ms):
        self.folder = folder
        self.ms = ms
        self.events = []        # (block, text)
        self.frames = {}        # file name -> hash of its pixels
        self.peaks = []
        self.sound_hash = None
        self.sound_start = ms   # emulated ms of the recording's first sample
        self.failure = None     # why the emulator stopped early, if it did
        self.stop = None        # the emulator's "stop: ..." line

    @classmethod
    def from_output(cls, folder, ms, stop):
        run = cls(folder, ms)
        lines = (folder / "run.log").read_text(errors="replace").splitlines()
        for line in lines:
            m = re.match(r"\[block (\d+)\] (.*)", line)
            if m and "unique blocks" not in m.group(2):     # progress lines
                run.events.append((int(m.group(1)), m.group(2)))
            elif line.startswith("stop: "):
                run.stop = line
        if not any(line.startswith(f"stop: {stop}") for line in lines):
            last = next((line for line in reversed(lines) if line.strip()), "no output")
            run.failure = f"the emulator stopped early: {last.strip()} (see {(folder / 'run.log').relative_to(ROOT)})"
        for path in sorted((folder / "frames").glob("*.png")):
            run.frames[path.name] = pixels_hash(path)
        wav = folder / "audio.wav"
        if wav.exists():
            with wave.open(str(wav)) as w:
                rate, data = w.getframerate(), w.readframes(w.getnframes())
            samples = array("h", data)
            run.sound_hash = hashlib.sha256(data).hexdigest()
            run.sound_start = ms - len(samples) // 2 * 1000 // rate
            step = 2 * rate * PEAK_MS // 1000
            run.peaks = [max(map(abs, samples[i:i + step])) for i in range(0, len(samples), step)]
        return run

    @classmethod
    def from_reference(cls, folder, ms):
        run = cls(folder, ms)
        for line in (folder / "events.txt").read_text().splitlines():
            block, text = line.split(" ", 1)
            if text.startswith("stop: "):
                run.stop = text
            else:
                run.events.append((int(block), text))
        info = json.loads((folder / "run.json").read_text())
        run.frames, run.peaks = info["frames"], info["peaks"]
        run.sound_hash, run.sound_start = info["sound_hash"], info["sound_start"]
        return run

    def save_reference(self, folder):
        shutil.rmtree(folder, ignore_errors=True)
        (folder / "frames").mkdir(parents=True)
        (folder / "events.txt").write_text("".join(f"{b} {t}\n" for b, t in self.events) + f"0 {self.stop}\n")
        (folder / "run.json").write_text(json.dumps(dict(
            frames=self.frames, peaks=self.peaks, sound_hash=self.sound_hash,
            sound_start=self.sound_start), indent=0))
        for name in self.frames:
            shutil.copy(self.folder / "frames" / name, folder / "frames" / name)

    def frame_times(self):
        """Screen file name -> emulated ms it appeared at."""
        times = {}
        for block, text in self.events:
            m = re.match(r"lcd: new frame (\d+)", text)
            if m:
                times[f"frame_{int(m.group(1)):04d}.png"] = block // BLOCKS_PER_MS
        return times

    def peak(self, lo, hi):
        """The loudest sample between emulated ms lo and hi."""
        a = max(0, (lo - self.sound_start) // PEAK_MS)
        b = max(0, (hi - self.sound_start) // PEAK_MS)
        return max(self.peaks[a:b], default=0)

    def peak_at(self, ms):
        i = (ms - self.sound_start) // PEAK_MS
        return self.peaks[i] if 0 <= i < len(self.peaks) else 0


def pixels_hash(path):
    with Image.open(path) as img:
        return hashlib.sha256(img.convert("RGB").tobytes()).hexdigest()


def seconds(ms):
    return f"{ms / 1000:.1f} s"


def check(run, checks):
    """Failures of the scenario's own checks."""
    for what, lo, hi in checks:
        if what in ("loud", "quiet"):
            peak = run.peak(lo, hi)
            if (peak < LOUD) if what == "loud" else (peak > QUIET):
                yield f"sound should be {what} from {seconds(lo)} to {seconds(hi)} (peak {peak})"
        elif not any(lo <= block // BLOCKS_PER_MS <= hi and re.search(what, text)
                     for block, text in run.events):
            yield f"no {what!r} between {seconds(lo)} and {seconds(hi)}"


def compare(new, ref):
    """How the new run differs from the reference."""
    if new.stop != ref.stop:
        yield f"the run ended with {new.stop!r}, expected {ref.stop!r}"
    for a, b in zip(ref.events, new.events):
        if a != b:
            yield (f"log differs: expected {a[1]!r} at {seconds(a[0] / BLOCKS_PER_MS)}, "
                   f"got {b[1]!r} at {seconds(b[0] / BLOCKS_PER_MS)}")
            break
    else:
        if len(ref.events) != len(new.events):
            yield f"log has {len(new.events)} lines, expected {len(ref.events)}"
    changed = sorted(n for n in ref.frames.keys() | new.frames.keys()
                     if ref.frames.get(n) != new.frames.get(n))
    if changed:
        first = changed[0]
        times = new.frame_times() if first in new.frames else ref.frame_times()
        detail = f"first {first} at {seconds(times.get(first, 0))}"
        if first in new.frames and first in ref.frames:
            image = side_by_side(ref.folder / "frames" / first, new.folder / "frames" / first, new.folder)
            detail += f", see {image.relative_to(ROOT)}"
        yield f"{len(changed)} of {len(ref.frames)} screens differ, {detail}"
    if new.sound_hash != ref.sound_hash:
        start = min(new.sound_start, ref.sound_start)
        where = next((ms for ms in range(start, ref.ms, PEAK_MS) if new.peak_at(ms) != ref.peak_at(ms)), None)
        yield (f"sound differs from {seconds(where)}" if where is not None
               else "sound differs, by less than its peak levels show")


def side_by_side(ref_path, new_path, folder):
    """Reference, new and their difference (amplified) in one image."""
    ref, new = Image.open(ref_path).convert("RGB"), Image.open(new_path).convert("RGB")
    diff = ImageChops.difference(ref, new).point(lambda v: min(255, v * 4))
    out = Image.new("RGB", (ref.width * 3 + 8, ref.height), "white")
    for i, img in enumerate((ref, new, diff)):
        out.paste(img, (i * (ref.width + 4), 0))
    path = folder / f"diff_{new_path.name}"
    out.save(path)
    return path


def run_scenario(name):
    sc = SCENARIOS[name]
    folder = OUT / name
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    cmd = [sys.executable, "-I", str(ROOT / "emu.py"), "--rtc-time", sc["rtc"],
           "--max-blocks", str(sc["ms"] * BLOCKS_PER_MS), "--keys", sc["keys"], "--out", str(folder)]
    start = time.monotonic()
    with open(folder / "run.log", "w") as log:
        subprocess.run(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    return Run.from_output(folder, sc["ms"], sc.get("stop", "block budget")), time.monotonic() - start


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("names", nargs="*", metavar="NAME", help=f"scenarios to run (default: all of {', '.join(SCENARIOS)})")
    p.add_argument("--update", action="store_true", help="record the runs as the new reference")
    args = p.parse_args()
    names = args.names or list(SCENARIOS)
    for name in names:
        if name not in SCENARIOS:
            p.error(f"unknown scenario {name!r}")
    with concurrent.futures.ThreadPoolExecutor() as pool:
        results = dict(zip(names, pool.map(run_scenario, names)))
    failed = False
    for name in names:
        run, wall = results[name]
        problems = [run.failure] if run.failure else list(check(run, SCENARIOS[name]["checks"]))
        ref_folder = REFERENCE / name
        if args.update or run.failure:
            if args.update and not problems:
                run.save_reference(ref_folder)
        elif not (ref_folder / "run.json").exists():
            problems.append("no reference yet: record one with make test-update")
        else:
            problems += compare(run, Run.from_reference(ref_folder, run.ms))
        failed |= bool(problems)
        status = "FAIL" if problems else "recorded" if args.update else "ok"
        print(f"{name:6} {status:8} "
              f"{len(run.frames)} screens, {seconds(run.ms)} emulated in {wall:.1f} s: {SCENARIOS[name]['about']}")
        for problem in problems:
            print(f"       {problem}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
