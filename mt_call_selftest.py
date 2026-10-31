"""Self-test of mt_call.py / app_dispatch.py on the LughOS phone image (needs the image's tel_bridge TEL log).

  python mt_call_selftest.py [--emu DIR] [--dump DIR] [--out DIR]

--emu: the emulator checkout under test (default: this file's directory); --dump: the phone image (default
/Users/conornash/Documents/nokia105/work/lughos-next/dump); --out: scratch (default /tmp/mt_call_selftest).
Runs, in parallel, on the phone dump with --sim:

  answer:  --inject-call 25000:07700900461, key select at 27000 (the shell's Answer),
           --inject-call-event 30000:disc:16, to 31000 ms
  connect: the same answer, then --inject-call-event 28000:connect, to 28500 ms (separate run: after a
           shell answer, 0xa159 is followed within ~1 s by a restart into the boot ROM, pc 0x000176eb at
           29087 ms then a loop at 0x00012046; MN state is not modelled, see mt_call.py)
  overlap: --inject-sms 25000 + --inject-call 25100 (100 ms apart), --inject-sms 26000 + --inject-call 26000
           (same ms), to 27500 ms

and checks:
  A1 inject-call.log has "call ConnectCallEx(dual_sys=0 call_id=0" between 27000 and 28000 ms
  A3 the PSRAM log (nokia105/tools/lughlog.py, tag ?23) has TEL a 0x0000 b 0x0000a15b at >= 30000 ms, its W1 line a 0x0200 b 0x00000010 (cause 16)
  B1 ... TEL a 0x0001 b 0x0000a1a7 twice (both texts reached the shell)
  B2 ... TEL a 0x0000 b 0x0000a15d twice (both calls reached the shell)
  B3 every emulator run exited 0
  C1 (connect run) TEL a 0x0000 b 0x0000a159 at >= 28000 ms (the connect reached the shell)
Exit 0 only if every check passes.  Each check prints PASS or FAIL with its evidence.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOKIA105 = Path("/Users/conornash/Documents/nokia105")
PY = Path("/Users/conornash/Documents/nokia105-emu/.venv/bin/python")
BPM = 4000
RTC = "2026-10-30T21:47:00"
TEL = re.compile(r"\s(\d+) ms\s+\?23\s+a 0x([0-9a-f]{4}) b 0x([0-9a-f]{8})")

RUNS = {
    "answer": (31000, ["--inject-call", "25000:07700900461", "--keys", "27000:select",
                       "--inject-call-event", "30000:disc:16"]),
    "connect": (28500, ["--inject-call", "25000:07700900461", "--keys", "27000:select",
                        "--inject-call-event", "28000:connect"]),
    "overlap": (27500, ["--inject-sms", "25000:+447700900123:Hi", "--inject-call", "25100:07700900461",
                        "--inject-sms", "26000:+447700900123:Same", "--inject-call", "26000:07700900123"]),
}


def cmd(emu, dump, out, end_ms, extra):
    c = [str(PY), "-I", str(emu / "emu.py"), "--lcd-id", "0x7c89f0", "--power-key-ms", "8000",
         "--poll-limit", "40000000", "--dump", str(dump), "--out", str(out), "--max-blocks", str(end_ms * BPM),
         "--rtc-time", RTC, "--sim"]
    rom = dump / "bootrom.bin"
    if not rom.exists() or rom.read_bytes().count(0) == rom.stat().st_size:
        c += ["--start", "flash"]
    return c + extra


def tel(out):
    if not (out / "psram.bin").exists():
        return []
    r = subprocess.run(["uv", "run", str(NOKIA105 / "tools/lughlog.py"), str(out / "psram.bin")],
                       cwd=str(NOKIA105), capture_output=True, text=True)
    return [(int(ms), a, b) for ms, a, b in TEL.findall(r.stdout)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emu", type=Path, default=HERE)
    ap.add_argument("--dump", type=Path, default=NOKIA105 / "work/lughos-next/dump")
    ap.add_argument("--out", type=Path, default=Path("/tmp/mt_call_selftest"))
    a = ap.parse_args()
    procs = {}
    for name, (end, extra) in RUNS.items():
        out = a.out / name
        subprocess.run(["rm", "-rf", str(out)], check=True)
        out.mkdir(parents=True)
        log = open(out / "emu.log", "w")
        c = cmd(a.emu.resolve(), a.dump, out, end, extra)
        print(f"run {name}: {' '.join(c)}", flush=True)
        procs[name] = (subprocess.Popen(c, cwd=str(a.emu), stdout=log, stderr=subprocess.STDOUT), out)
    rc = {n: p.wait() for n, (p, _) in procs.items()}
    res = []

    def check(tag, ok, detail):
        res.append(ok)
        print(f"{'PASS' if ok else 'FAIL'} {tag}: {detail}", flush=True)

    for n, (_, out) in procs.items():
        last = (out / "emu.log").read_text(errors="replace").strip().splitlines()[-1:] or [""]
        print(f"run {n}: exit {rc[n]}; emu.log last line: {last[0][:200]}")

    out = procs["answer"][1]
    clog = out / "inject-call.log"
    lines = clog.read_text().splitlines() if clog.exists() else []
    conn = [ln for ln in lines if "call ConnectCallEx(dual_sys=0 call_id=0" in ln
            and 27000 <= float(ln.split()[0]) < 28000]
    check("A1 ConnectCallEx logged after the answer key", rc["answer"] == 0 and bool(conn),
          conn[0] if conn else f"no such line in {clog} ({len(lines)} lines)")
    t = tel(out)
    disc = [i for i, x in enumerate(t) if x[1] == "0000" and x[2] == "0000a15b" and x[0] >= 30000]
    w1 = [t[j] for i in disc for j in range(i + 1, min(i + 3, len(t))) if t[j][1] == "0200"]
    check("A3 DISCONNECTED_IND 0xa15b cause 16 reached the shell", bool(disc) and bool(w1) and w1[0][2] == "00000010",
          f"{[t[i] for i in disc][:1]} W1 {w1[:1]}" if disc else f"no TEL a 0x0000 b 0x0000a15b ({len(t)} TEL lines)")

    out = procs["overlap"][1]
    t = tel(out)
    sms = [x for x in t if x[1] == "0001" and x[2] == "0000a1a7"]
    calls = [x for x in t if x[1] == "0000" and x[2] == "0000a15d"]
    check("B1 both texts reached the shell", len(sms) == 2, f"{len(sms)} x TEL a 0x0001 b 0x0000a1a7: {sms}")
    check("B2 both calls reached the shell", len(calls) == 2, f"{len(calls)} x TEL a 0x0000 b 0x0000a15d: {calls}")
    check("B3 every run exited 0", all(v == 0 for v in rc.values()), str(rc))
    t = tel(procs["connect"][1])
    sc = [x for x in t if x[1] == "0000" and x[2] == "0000a159" and x[0] >= 28000]
    check("C1 SETUP_COMPLETE_IND 0xa159 reached the shell", bool(sc), str(sc[:1]) if sc else "no TEL a 0x0000 b 0x0000a159")
    print("SELFTEST " + ("PASS" if all(res) else "FAIL"))
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
