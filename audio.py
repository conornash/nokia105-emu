"""Sound: the DMA controller, the voice-band codec (VBC) and audio output.

The MMI mixes its sounds (ringtones, tones) on the ARM into 16-bit PCM
buffers, and the DMA controller feeds them to the VBC's DAC FIFOs:

DMA controller (0x20100000, INTC line 31; register layout as in Linux's
sprd-dma driver; firmware driver flash 0x1d4c8..0x1da9a):
  global +0x10   channels with a pending interrupt (ISR flash 0x1d55c)
  channel n at 0x20101000 + 0x40 * n:
    +0x00 pause: bit 0 request, bit 16 paused (polled by flash 0x1d7ac)
    +0x08 config: bit 0 enable
    +0x0c interrupts: enable bits 0-3 (fragment, block, transaction, list),
          masked status 8-11, raw status 16-19, write-1 clear 24-27
    +0x10 source, +0x14 destination
    +0x18 bits 0-16 fragment length, 20-21 fixed address, 28-31 widths
    +0x1c block length
  The audio channels move one 320-byte block (160 samples) from a PSRAM
  buffer to a FIFO and raise the block interrupt. The hardware clears the
  enable bit when the block is done: after each block the driver rewrites
  +0x08 twice with the value it reads (flash 0x1d770, 0x1d794) before the
  write that restarts the channel (flash 0x1d884).

VBC (0x82003000, the "ARM_VB" voice-band codec of older Spreadtrum chips):
  +0x00 DAC 0 FIFO (left), +0x04 DAC 1 FIFO (right), one 16-bit sample per
  write; +0x10 FIFO sizes (kern 0x407f452: DAC size - 1 in bits 8-15, at
  most 160); +0x40.. gain and filter settings (not modelled). Before
  starting the DMA the driver primes both FIFOs with silence (kern
  0x407f492).
The DAC plays 32000 samples a second. This was measured, not read from a
register: played at 48 kHz, every note of Nokia Tune came out exactly 1.5x
sharp (E6 at 1978 Hz instead of 1318.5, the whole tune 2.4 s instead of
3.6 s), and at 32 kHz they are in tune. The firmware resamples its 22.05 kHz
MIDI output in software instead of reprogramming the DAC, which suggests
the rate is fixed; no register selecting it was found.

The audio power-up sequence also waits on a small analogue control block
at 0x8a002000 (INTC line 16; pointer at RAM 0x4302ca0, helpers flash
0x224d0..0x225d2): after a ramp it starts through bit 14 of +0x0c, the
power state machine (kern 0x4066e66, scenario 13) enables the block's two
interrupts (+0x28 bits 0-1) and arms a 600 ms timer. Status +0x20 bit 5
is the one that ends the wait: the deferred handler (kern 0x4066ffc) calls
the timer's own callback for it. +0x24 is write-1-to-clear. Without the
interrupt every sound starts 600 ms late; the emulator raises it 20 ms
after it is enabled (the real ramp time is not known).

Each FIFO is modelled as a timeline: samples queue up behind the ones
already waiting and play at the sample rate. A DMA block completes when
its last sample would enter the 160-sample FIFO, which paces the guest's
mixer at the real sample rate. Output is written up to the current
emulated time to out/audio.wav and, in the live window, to ffplay through
LivePlayer.
"""
import shutil
import subprocess
import threading
import time
import wave
from array import array

DMA_BASE = 0x20100000
DMA_INT_STS = DMA_BASE + 0x10
CH_BASE = DMA_BASE + 0x1000
CH_STRIDE = 0x40
CHANNELS = 32
PAUSE, REQ, CFG, INTC, SRC, DST, FRAG, BLK = 0x00, 0x04, 0x08, 0x0c, 0x10, 0x14, 0x18, 0x1c
PAUSE_REQ, PAUSE_DONE = 1 << 0, 1 << 16
CFG_EN = 1 << 0
INT_FRAG, INT_BLK, INT_TRSC = 1 << 0, 1 << 1, 1 << 2
INT_STS_SHIFT, INT_RAW_SHIFT, INT_CLR_SHIFT = 8, 16, 24
IRQ_LINE = 31

AUD_CTL = 0x8a002000
AUD_STS, AUD_CLR, AUD_EN = AUD_CTL + 0x20, AUD_CTL + 0x24, AUD_CTL + 0x28
AUD_DONE = 1 << 1           # status bit 5
AUD_IRQ_LINE = 16
AUD_RAMP_MS = 20

VBC_BASE = 0x82003000
VBC_DA = (VBC_BASE + 0x00, VBC_BASE + 0x04)
FIFO_SAMPLES = 160
OUT_RATE = 32000


class Vbc:
    """The two DAC FIFOs as sample timelines, written out as stereo PCM.

    Sample positions are absolute indexes at OUT_RATE counted from the start
    of the run. end[ch] is where the samples queued on DAC ch run out; new
    samples queue behind it, or start now if it has run dry."""

    def __init__(self, machine, wav_path):
        self.m = machine
        self.wav_path = wav_path
        self.live = False           # live.py: also play through ffplay
        self.written = 0            # frames before this index are written out
        self.pending = array("h")   # stereo frames from `written` on
        self.end = [0, 0]
        self.loud_until = 0         # where the last non-silent samples end
        self.samples = 0            # samples received, both DACs
        self.wav = None
        self.player = None

    def now(self):
        return self.m.nblocks * OUT_RATE // (1000 * self.m.blocks_per_ms)

    def push(self, ch, samples):
        """Queue samples on DAC ch. Returns the block time at which the last
        one enters the FIFO, i.e. when a DMA block feeding them completes."""
        self.flush()
        start = max(self.end[ch], self.now())
        stop = start + len(samples)
        need = 2 * (stop - self.written) - len(self.pending)
        if need > 0:
            self.pending.extend(array("h", bytes(2 * need)))
        base = 2 * (start - self.written) + ch
        self.pending[base:base + 2 * len(samples):2] = array("h", samples)
        self.end[ch] = stop
        if any(samples):
            self.loud_until = max(self.loud_until, stop)
        self.samples += len(samples)
        return (stop - FIFO_SAMPLES) * 1000 * self.m.blocks_per_ms // OUT_RATE

    def sounding(self, margin_ms=200):
        """Whether something audible is playing now or ended within
        margin_ms (the gaps between notes)."""
        return self.loud_until + margin_ms * OUT_RATE // 1000 > self.now()

    def on_fifo_write(self, ch, value):
        sample = value & 0xffff
        self.push(ch, [sample - 0x10000 if sample & 0x8000 else sample])

    def flush(self):
        """Write out the frames up to now."""
        upto = self.now()
        n = upto - self.written
        if n <= 0:
            return
        # frames up to here hold queued samples; the rest is silence after them
        sounding = min(max(self.end), upto) - self.written
        frames = self.pending[:2 * n]
        del self.pending[:2 * n]
        if len(frames) < 2 * n:
            frames.extend(array("h", bytes(2 * (2 * n - len(frames)))))
        self.written = upto
        if not self.samples:
            return                  # nothing has played yet
        data = frames.tobytes()
        if self.wav is None:
            self.wav = wave.open(str(self.wav_path), "wb")
            self.wav.setnchannels(2)
            self.wav.setsampwidth(2)
            self.wav.setframerate(OUT_RATE)
        self.wav.writeframes(data)
        # the speaker only gets the queued part: the silence between sounds
        # arrives all at once with the next sound and would delay it
        if self.live and sounding > 0:
            self.play(data[:4 * sounding])

    def player_command(self):
        """ffplay reading raw stereo PCM from stdin, or None."""
        exe = shutil.which("ffplay")
        if not exe:
            print("audio: ffplay not found (install ffmpeg); sound goes to out/audio.wav only")
            return None
        return [exe, "-nodisp", "-autoexit", "-loglevel", "quiet", "-fflags", "nobuffer",
                "-probesize", "32", "-analyzeduration", "0", "-f", "s16le", "-ar", str(OUT_RATE),
                "-ch_layout", "stereo", "-i", "-"]

    def play(self, data):
        if self.player is None:
            cmd = self.player_command()
            if not cmd:
                self.live = False
                return
            self.player = LivePlayer(cmd)
        self.player.feed(data)

    def close(self):
        self.flush()
        if self.wav:
            self.wav.close()
        if self.player:
            self.player.close()


class LivePlayer:
    """Feeds ffplay a continuous real-time stream, the way a sound card is
    fed: every 10 ms a thread sends the next 10 ms, the phone's samples when
    there are any and silence otherwise. ffplay does not play short bursts
    with gaps between them reliably, which is all the phone produces for a
    key beep. Samples are queued for at most MAX_QUEUE_MS, so latency stays
    bounded when the emulator delivers them in a rush."""

    TICK_MS = 10
    LEAD_MS = 30            # stay this far ahead of the wall clock
    MAX_QUEUE_MS = 150

    def __init__(self, cmd):
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        self.queue = bytearray()
        self.lock = threading.Lock()
        self.running = True
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def feed(self, data):
        with self.lock:
            self.queue += data
            excess = len(self.queue) - 4 * OUT_RATE * self.MAX_QUEUE_MS // 1000
            if excess > 0:
                del self.queue[:excess]

    def run(self):
        start, sent = time.monotonic(), 0
        while self.running:
            due = int((time.monotonic() - start) * OUT_RATE) + OUT_RATE * self.LEAD_MS // 1000
            n = due - sent
            if n > 0:
                with self.lock:
                    take = min(len(self.queue) // 4, n)
                    data = bytes(self.queue[:4 * take])
                    del self.queue[:4 * take]
                try:
                    self.proc.stdin.write(data + bytes(4 * (n - take)))
                    self.proc.stdin.flush()
                except OSError:
                    return
                sent = due
            time.sleep(self.TICK_MS / 1000)

    def close(self):
        self.running = False
        self.thread.join(timeout=1)
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.terminate()


class Dma:
    def __init__(self, machine, vbc):
        self.m = machine
        self.vbc = vbc
        self.raw = [0] * CHANNELS       # raw interrupt bits per channel
        self.generation = [0] * CHANNELS  # bumped when a channel stops
        self.busy = [False] * CHANNELS
        self.blocks = 0

    def reg(self, ch, off):
        return self.m.regs.get(CH_BASE + CH_STRIDE * ch + off, 0)

    def read_pause(self, ch):
        v = self.reg(ch, PAUSE)
        return v | PAUSE_DONE if v & PAUSE_REQ else v & ~PAUSE_DONE

    def read_intc(self, ch):
        enable = self.reg(ch, INTC) & 0x1f
        raw = self.raw[ch]
        return enable | (raw & enable) << INT_STS_SHIFT | raw << INT_RAW_SHIFT

    def read_status(self):
        bits = 0
        for ch in range(CHANNELS):
            if self.raw[ch] & self.reg(ch, INTC) & 0x1f:
                bits |= 1 << ch
        return bits

    def on_intc(self, ch, value):
        self.raw[ch] &= ~(value >> INT_CLR_SHIFT & 0x1f)
        self.m.regs[CH_BASE + CH_STRIDE * ch + INTC] = value & 0x1f
        self.update_line()

    def update_line(self):
        self.m.intc.set_line(IRQ_LINE, self.read_status())

    def on_cfg(self, ch, value):
        if not value & CFG_EN or self.reg(ch, PAUSE) & PAUSE_REQ:
            if self.busy[ch]:           # stopped: drop its pending completion
                self.busy[ch] = False
                self.generation[ch] += 1
            return
        if self.busy[ch]:
            return                      # already running
        dst = self.reg(ch, DST)
        if dst not in VBC_DA:
            return          # only audio transfers are modelled
        length = self.reg(ch, BLK) & 0x1ffff or self.reg(ch, FRAG) & 0x1ffff
        data = bytes(self.m.uc.mem_read(self.reg(ch, SRC), length))
        due = self.vbc.push(VBC_DA.index(dst), array("h", data))
        self.blocks += 1
        self.busy[ch] = True
        gen = self.generation[ch]
        self.m.schedule(max(1, due - self.m.nblocks), lambda: self.block_done(ch, gen))

    def block_done(self, ch, gen):
        if gen != self.generation[ch]:
            return
        self.busy[ch] = False
        self.m.regs[CH_BASE + CH_STRIDE * ch + CFG] &= ~CFG_EN
        self.raw[ch] |= INT_FRAG | INT_BLK | INT_TRSC
        self.update_line()


class AudCtl:
    """The analogue control block at 0x8a002000: see the module docstring."""

    def __init__(self, machine):
        self.m = machine
        self.flags = 0          # bit 0 -> status bit 4, bit 1 -> status bit 5
        self.ramps = 0

    def read_status(self):
        return self.m.regs.get(AUD_STS, 0) & 3 | self.flags << 4

    def on_clear(self, value):
        self.flags &= ~value & 3
        self.update_line()

    def on_enable(self, value):
        if value & AUD_DONE and not self.flags & AUD_DONE:
            self.m.schedule(AUD_RAMP_MS * self.m.blocks_per_ms, self.ramp_done)
        self.update_line()

    def ramp_done(self):
        self.flags |= AUD_DONE
        self.ramps += 1
        self.update_line()

    def update_line(self):
        self.m.intc.set_line(AUD_IRQ_LINE, self.flags & self.m.regs.get(AUD_EN, 0))
