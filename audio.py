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
The DAC sample rate is the PMIC codec register 0x82001380 (0xbb80 =
48000 Hz while a ringtone plays).

Each FIFO is modelled as a timeline: samples queue up behind the ones
already waiting and play at the sample rate. A DMA block completes when
its last sample would enter the 160-sample FIFO, which paces the guest's
mixer at the real sample rate. Output is written up to the current
emulated time to out/audio.wav and, in the live window, to ffplay.
"""
import shutil
import subprocess
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

VBC_BASE = 0x82003000
VBC_DA = (VBC_BASE + 0x00, VBC_BASE + 0x04)
FIFO_SAMPLES = 160
RATE_REG = 0x82001380
OUT_RATE = 48000


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
        self.samples = 0            # samples received, both DACs
        self.wav = None
        self.player = None
        self.rate_warned = False

    def now(self):
        return self.m.nblocks * OUT_RATE // (1000 * self.m.blocks_per_ms)

    def push(self, ch, samples):
        """Queue samples on DAC ch. Returns the block time at which the last
        one enters the FIFO, i.e. when a DMA block feeding them completes."""
        rate = self.m.regs.get(RATE_REG, OUT_RATE)
        if rate not in (0, OUT_RATE) and not self.rate_warned:   # 0: not set up yet
            self.rate_warned = True
            print(f"[block {self.m.nblocks}] audio: DAC rate {rate} Hz, played as {OUT_RATE} Hz")
        self.flush()
        start = max(self.end[ch], self.now())
        stop = start + len(samples)
        need = 2 * (stop - self.written) - len(self.pending)
        if need > 0:
            self.pending.extend(array("h", bytes(2 * need)))
        base = 2 * (start - self.written) + ch
        self.pending[base:base + 2 * len(samples):2] = array("h", samples)
        self.end[ch] = stop
        self.samples += len(samples)
        return (stop - FIFO_SAMPLES) * 1000 * self.m.blocks_per_ms // OUT_RATE

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
        return [exe, "-nodisp", "-loglevel", "quiet", "-fflags", "nobuffer", "-probesize", "32",
                "-analyzeduration", "0", "-f", "s16le", "-ar", str(OUT_RATE),
                "-ch_layout", "stereo", "-i", "-"]

    def play(self, data):
        if self.player is None:
            cmd = self.player_command()
            if not cmd:
                self.live = False
                return
            self.player = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        try:
            self.player.stdin.write(data)
            self.player.stdin.flush()
        except (BrokenPipeError, OSError):
            self.live = False

    def close(self):
        self.flush()
        if self.wav:
            self.wav.close()
        if self.player:
            try:
                self.player.stdin.close()
            except OSError:
                pass
            self.player.terminate()


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
