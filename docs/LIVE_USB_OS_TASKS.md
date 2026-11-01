# Live USB Loader + Telephony Inspection Plan (`tracker.db` Specification)

Target tracker: `/Users/conornash/Documents/lughos/tracker.db`  
Target repos: `/Users/conornash/Documents/lughos`, `/Users/conornash/Documents/nokia105`, `/Users/conornash/Documents/nokia105-emu`

---

## 1. Plain-Language Summary (ASD-STE100)

### Goal
Run new code and inspect the live telephony stack on the Nokia 105 hardware over a **normal USB cable**, without flashing or rebooting the phone for each change.

### Why we do it this way
1. **Keep the factory OS and radio running:** The cellular radio and telephony stack make more than 1,100 calls into the factory OS. We cannot replace the factory OS without breaking calls and texts. Instead, LughOS runs inside the factory OS.
2. **Use a normal USB cable after boot:** The special OTG boot cable forces the silicon chip into loader mode at power-on before the OS can start. A normal USB cable lets the phone boot the factory OS, SIM card, and cellular radio normally.
3. **Start a USB listener inside LughOS:** We flash the phone **one time** with a LughOS build that turns on the USB data port after the phone boots.
4. **Send commands and code over USB into RAM:** While the telephony stack is running, the computer sends commands over USB to:
   - Read and write memory (`PEEK` and `POKE`),
   - Pull live telephony logs and screen frames (`LOG_DRAIN`),
   - Press keys (`KEY_INJECT`),
   - Upload new code into RAM and run it immediately (`EXEC_RAM`).

---

## 2. Schema Reference (`/Users/conornash/Documents/lughos/tracker.db`)

Both tables in `/Users/conornash/Documents/lughos/tracker.db` are `STRICT`:

```sql
CREATE TABLE task (
  id INTEGER PRIMARY KEY,
  app TEXT NOT NULL,
  title TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN
    ('planned','d2-drawn','in-progress','emu-verified','in-image',
     'handset-check','handset-pass','handset-fail','blocked')),
  d2_node TEXT,
  goal TEXT,
  source_note TEXT,
  created TEXT NOT NULL,
  updated TEXT NOT NULL
) STRICT;

CREATE TABLE test (
  id INTEGER PRIMARY KEY,
  app TEXT NOT NULL,
  title TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('PASS','FAIL','N/A','BROKEN')),
  d2_node TEXT,
  goal TEXT,
  test_program TEXT,
  created TEXT NOT NULL,
  updated TEXT NOT NULL
) STRICT;
```

Current maximum IDs in `tracker.db` (as of 2026-11-01):
- `MAX(task.id) = 75` → new tasks use IDs **76..80**
- `MAX(test.id) = 75` → new test rows use IDs **76..85**
- Highest test case file across all worktrees is `t222` → new test case files use **`t230`..`t239`**

---

## 3. Tasks (`task` Table Rows 76–80)

| `id` | `app` | `state` | `d2_node` | `title` | `goal` | `source_note` |
|---:|---|---|---|---|---|---|
| **76** | `investigation` | `planned` | `""` | Live USB U0: map the stock USB service (`T_USBSERVICE_HANDLER`, `UCOM_MAIN_HANDLER`, `0x20300000`) and choose the in-OS USB transport | A report (`work/reports/inv-usb-live.md`) gives the exact addresses and states whether LughOS uses stock `UCOM` or drives `0x20300000` directly on a plain USB cable | `work/reports-archive/inv-hitl.md` Q3 rows 16-23: `UCOM_MAIN_HANDLER` (`kern 0x0403a4ee`), `T_USBSERVICE_HANDLER` (`0x0403b5bc`), `USB Detect Timer` (`0x0402540c`), and `nor_fdl1` USB driver (`0x20300000`). Settle whether stock `UCOM` can be started from `P_APP` or if polling/IRQ on `0x20300000` is cleaner. |
| **77** | `port` | `planned` | `phone: seam.usb` | Live USB U1: model the USB device pipe in `nokia105-emu` (`--usb-script`) and add `seam.usb` to `docs/flows/phone.d2` | In the emulator, a host packet sent to the USB pipe reaches LughOS and returns an `ACK` frame both on standby and during a ringing call | `nokia105-emu` currently models no USB (`README.md`: "No camera or USB"). Add `usb_dev.py` (`0x20300000` / UCOM hook) and `--usb-script` so `tests/emu` can test live USB commands deterministically alongside `--inject-call` and `--inject-sms`. |
| **78** | `port` | `planned` | `phone: seam.usb` | Live USB U2: in-OS inspection responder (`PEEK`, `POKE`, `LOG_DRAIN`, `KEY_INJECT`) and host CLI (`nokia105/bin/usb-live.py`) | While LughOS runs, the host reads RAM/framebuffer, drains `LUGHLOG1` and `tel_bridge` events live, and injects keys without resetting the phone | Replaces the reset/power-off requirement of `bin/pull-log.ysh` and `work/reports-archive/log-readback.md`. `LOG_DRAIN` reads `core.host.log` (`0x043ae3f4`) and `tel_bridge` live while `P_MN` stays up. |
| **79** | `port` | `planned` | `phone: seam.usb` | Live USB U3: in-OS RAM code loader (`UPLOAD_RAM` + `EXEC_RAM`) with D-cache flush, I-cache invalidate, and `tel_bridge` API table | The host uploads a small ARM/Thumb binary into a PSRAM arena over USB, runs it on `P_APP` with access to `tel_bridge` and `MNCALL_*`, and gets the return value over USB with no flash or reboot | Uses the same CP15 D-cache clean + I-cache invalidate sequence that `nokia105/lugh/shell/stub.c` uses after relocating `.core`. Passes a pointer to the live API table (`log_put`, `tel_bridge_*`, `MNCALL_*`, `MNSMS_*`). |
| **80** | `phone` | `planned` | `phone: seam.usb` | Live USB U4: silicon proof on TA-1557 with a normal USB cable and live cellular network | Flash once, boot on battery, plug in a normal USB cable: `usb-live.py` drains live call/SMS logs and runs an `EXEC_RAM` telephony probe during a real call without rebooting | Depends on tasks 76-79. One flash via `bin/hitl-cycle.ysh`; all subsequent telephony experiments run in RAM over `bin/usb-live.py` on a plain data cable. |

---

## 4. Tests (`test` Table Rows 76–85, Cases `t230`–`t239`)

Every test follows the rules in `tests/emu/lib.py`, `tools/ctxpack.ysh`, and `tools/audit.ysh`:
- Each test is a file `tests/emu/cases/<title>.py` and is listed in `tests/emu/expected_cases.txt`.
- Each test's `D2_NODE` starts with `phone: ` and references nodes (`home`, `seam.usb`, `incall.incoming`, `incall`) and `# T-usb-<task_id><letter>` comments defined in `docs/flows/phone.d2`.

| `id` | `app` | `title` (case stem) | Initial `state` | `d2_node` | `goal` | `test_program` |
|---:|---|---|---|---|---|---|
| **76** | `phone` | `t230_usb_ping_while_idle` | `FAIL` | `phone: home -Select-> seam.usb (task 77, T-usb-77a)` | `[design @master] A host PING packet over the USB seam on standby returns PONG with the running protocol version (targets: design,phone) -- RED until task 77 (seam.usb)` | `LUGHOS_TARGET=phone tests/emu/run_one.py t230_usb_ping_while_idle` |
| **77** | `phone` | `t231_usb_ping_during_incoming_call` | `FAIL` | `phone: any -incoming-call-> incall.incoming -Select-> seam.usb (task 77, T-usb-77b)` | `[phone @master] While an incoming call rings (0xa15d), the USB responder answers PING without dropping the ring or losing focus (targets: phone) -- RED until task 77 (seam.usb)` | `LUGHOS_TARGET=phone tests/emu/run_one.py t231_usb_ping_during_incoming_call` |
| **78** | `phone` | `t232_usb_peek_reads_lughlog_and_fb` | `FAIL` | `phone: home -Select-> seam.usb (task 78, T-usb-78a)` | `[phone @master] A host PEEK over USB reads the LUGHLOG1 header at 0x043ae3f4 and the RGB565 framebuffer at 0x0415fb00 while the phone stays on standby (targets: phone) -- RED until task 78 (usb_peek)` | `LUGHOS_TARGET=phone tests/emu/run_one.py t232_usb_peek_reads_lughlog_and_fb` |
| **79** | `phone` | `t233_usb_log_drain_streams_call_and_sms` | `FAIL` | `phone: any -incoming-call-> incall.incoming -Select-> seam.usb (task 78, T-usb-78b)` | `[phone @master] During a live MT SMS (0xa1a7) and MT call (0xa15d), USB LOG_DRAIN returns the new LOG_TEL records over USB without resetting the phone (targets: phone) -- RED until task 78 (usb_log_drain)` | `LUGHOS_TARGET=phone tests/emu/run_one.py t233_usb_log_drain_streams_call_and_sms` |
| **80** | `phone` | `t234_usb_key_inject_answers_call` | `FAIL` | `phone: any -incoming-call-> incall.incoming -Select-> incall (task 78, T-usb-78c)` | `[phone @master] A USB KEY_INJECT Select command while ringing answers the call and transitions to incall, logging ConnectCallEx (targets: design,phone) -- RED until task 78 (usb_key_inject)` | `LUGHOS_TARGET=phone tests/emu/run_one.py t234_usb_key_inject_answers_call` |
| **81** | `phone` | `t235_usb_exec_ram_runs_probe_and_returns` | `FAIL` | `phone: home -Select-> seam.usb (task 79, T-usb-79a)` | `[phone @master] Uploading a Thumb probe via UPLOAD_RAM and invoking EXEC_RAM flushes caches, runs the probe in PSRAM, appends a custom LOG_TEL record, and returns its 32-bit result over USB (targets: phone) -- RED until task 79 (usb_exec_ram)` | `LUGHOS_TARGET=phone tests/emu/run_one.py t235_usb_exec_ram_runs_probe_and_returns` |
| **82** | `phone` | `t236_usb_exec_ram_calls_mncall_while_radio_up` | `FAIL` | `phone: home -Select-> seam.usb (task 79, T-usb-79b)` | `[phone @master] An uploaded RAM probe calls MNCALL_StartCallEx through the live API table over USB without flashing or rebooting, and the call log records the dial (targets: phone) -- RED until task 79 (usb_exec_ram)` | `LUGHOS_TARGET=phone tests/emu/run_one.py t236_usb_exec_ram_calls_mncall_while_radio_up` |
| **83** | `phone` | `t237_usb_exec_ram_rejects_bad_magic_or_oversize` | `FAIL` | `phone: home -Select-> seam.usb (task 79, T-usb-79c)` | `[design @master] UPLOAD_RAM/EXEC_RAM rejects payloads without a valid header magic or larger than the PSRAM scratch arena and leaves the phone running normally (targets: design,phone) -- RED until task 79 (usb_exec_ram)` | `LUGHOS_TARGET=phone tests/emu/run_one.py t237_usb_exec_ram_rejects_bad_magic_or_oversize` |
| **84** | `phone` | `t238_usb_live_session_survives_call_lifecycle` | `FAIL` | `phone: any -incoming-call-> incall.incoming -Select-> incall -End-> home (task 80, T-usb-80a)` | `[phone @master] Across a full call lifecycle (ring -> answer -> EXEC_RAM probe -> hangup -> standby), USB LOG_DRAIN and PEEK remain responsive with zero resets (targets: phone) -- RED until task 80 (usb_live_session)` | `LUGHOS_TARGET=phone tests/emu/run_one.py t238_usb_live_session_survives_call_lifecycle` |
| **85** | `phone` | `t239_usb_live_session_survives_sms_send_receive` | `FAIL` | `phone: home -Select-> seam.usb (task 80, T-usb-80b)` | `[phone @master] Across a live MT SMS arrival (0xa1a7) and an MO SMS send triggered via EXEC_RAM, USB LOG_DRAIN captures both events in one session without a reboot (targets: phone) -- RED until task 80 (usb_live_session)` | `LUGHOS_TARGET=phone tests/emu/run_one.py t239_usb_live_session_survives_sms_send_receive` |

---

## 5. Ready-to-Run SQL for the Operator Agent

Run this against `/Users/conornash/Documents/lughos/tracker.db` when ready to register the tasks and initial RED test rows:

```sql
BEGIN TRANSACTION;

INSERT INTO task (id, app, title, state, d2_node, goal, source_note, created, updated) VALUES
(76, 'investigation',
 'Live USB U0: map the stock USB service (T_USBSERVICE_HANDLER, UCOM_MAIN_HANDLER, 0x20300000) and choose the in-OS USB transport',
 'planned', '',
 'A report (work/reports/inv-usb-live.md) gives the exact addresses and states whether LughOS uses stock UCOM or drives 0x20300000 directly on a plain USB cable',
 'GAP: work/reports-archive/inv-hitl.md Q3 rows 16-23: UCOM_MAIN_HANDLER (kern 0x0403a4ee), T_USBSERVICE_HANDLER (0x0403b5bc), USB Detect Timer (0x0402540c), and nor_fdl1 USB driver (0x20300000). Settle whether stock UCOM can be started from P_APP or if polling/IRQ on 0x20300000 is cleaner.',
 '2026-11-01', '2026-11-01'),

(77, 'port',
 'Live USB U1: model the USB device pipe in nokia105-emu (--usb-script) and add seam.usb to docs/flows/phone.d2',
 'planned', 'phone: seam.usb',
 'In the emulator, a host packet sent to the USB pipe reaches LughOS and returns an ACK frame both on standby and during a ringing call',
 'GAP: nokia105-emu currently models no USB (README.md: No camera or USB). Add usb_dev.py (0x20300000 / UCOM hook) and --usb-script so tests/emu can test live USB commands deterministically alongside --inject-call and --inject-sms.',
 '2026-11-01', '2026-11-01'),

(78, 'port',
 'Live USB U2: in-OS inspection responder (PEEK, POKE, LOG_DRAIN, KEY_INJECT) and host CLI (nokia105/bin/usb-live.py)',
 'planned', 'phone: seam.usb',
 'While LughOS runs, the host reads RAM/framebuffer, drains LUGHLOG1 and tel_bridge events live, and injects keys without resetting the phone',
 'GAP: replaces the reset/power-off requirement of bin/pull-log.ysh and work/reports-archive/log-readback.md. LOG_DRAIN reads core.host.log (0x043ae3f4) and tel_bridge live while P_MN stays up.',
 '2026-11-01', '2026-11-01'),

(79, 'port',
 'Live USB U3: in-OS RAM code loader (UPLOAD_RAM + EXEC_RAM) with D-cache flush, I-cache invalidate, and tel_bridge API table',
 'planned', 'phone: seam.usb',
 'The host uploads a small ARM/Thumb binary into a PSRAM arena over USB, runs it on P_APP with access to tel_bridge and MNCALL_*, and gets the return value over USB with no flash or reboot',
 'GAP: uses the same CP15 D-cache clean + I-cache invalidate sequence that nokia105/lugh/shell/stub.c uses after relocating .core. Passes a pointer to the live API table (log_put, tel_bridge_*, MNCALL_*, MNSMS_*).',
 '2026-11-01', '2026-11-01'),

(80, 'phone',
 'Live USB U4: silicon proof on TA-1557 with a normal USB cable and live cellular network',
 'planned', 'phone: seam.usb',
 'Flash once, boot on battery, plug in a normal USB cable: usb-live.py drains live call/SMS logs and runs an EXEC_RAM telephony probe during a real call without rebooting',
 'GAP: depends on tasks 76-79. One flash via bin/hitl-cycle.ysh; all subsequent telephony experiments run in RAM over bin/usb-live.py on a plain data cable.',
 '2026-11-01', '2026-11-01');

INSERT INTO test (id, app, title, state, d2_node, goal, test_program, created, updated) VALUES
(76, 'phone', 't230_usb_ping_while_idle', 'FAIL',
 'phone: home -Select-> seam.usb (task 77, T-usb-77a)',
 '[design @master] A host PING packet over the USB seam on standby returns PONG with the running protocol version (targets: design,phone) -- RED until task 77 (seam.usb)',
 'LUGHOS_TARGET=phone tests/emu/run_one.py t230_usb_ping_while_idle',
 '2026-11-01', '2026-11-01'),

(77, 'phone', 't231_usb_ping_during_incoming_call', 'FAIL',
 'phone: any -incoming-call-> incall.incoming -Select-> seam.usb (task 77, T-usb-77b)',
 '[phone @master] While an incoming call rings (0xa15d), the USB responder answers PING without dropping the ring or losing focus (targets: phone) -- RED until task 77 (seam.usb)',
 'LUGHOS_TARGET=phone tests/emu/run_one.py t231_usb_ping_during_incoming_call',
 '2026-11-01', '2026-11-01'),

(78, 'phone', 't232_usb_peek_reads_lughlog_and_fb', 'FAIL',
 'phone: home -Select-> seam.usb (task 78, T-usb-78a)',
 '[phone @master] A host PEEK over USB reads the LUGHLOG1 header at 0x043ae3f4 and the RGB565 framebuffer at 0x0415fb00 while the phone stays on standby (targets: phone) -- RED until task 78 (usb_peek)',
 'LUGHOS_TARGET=phone tests/emu/run_one.py t232_usb_peek_reads_lughlog_and_fb',
 '2026-11-01', '2026-11-01'),

(79, 'phone', 't233_usb_log_drain_streams_call_and_sms', 'FAIL',
 'phone: any -incoming-call-> incall.incoming -Select-> seam.usb (task 78, T-usb-78b)',
 '[phone @master] During a live MT SMS (0xa1a7) and MT call (0xa15d), USB LOG_DRAIN returns the new LOG_TEL records over USB without resetting the phone (targets: phone) -- RED until task 78 (usb_log_drain)',
 'LUGHOS_TARGET=phone tests/emu/run_one.py t233_usb_log_drain_streams_call_and_sms',
 '2026-11-01', '2026-11-01'),

(80, 'phone', 't234_usb_key_inject_answers_call', 'FAIL',
 'phone: any -incoming-call-> incall.incoming -Select-> incall (task 78, T-usb-78c)',
 '[phone @master] A USB KEY_INJECT Select command while ringing answers the call and transitions to incall, logging ConnectCallEx (targets: design,phone) -- RED until task 78 (usb_key_inject)',
 'LUGHOS_TARGET=phone tests/emu/run_one.py t234_usb_key_inject_answers_call',
 '2026-11-01', '2026-11-01'),

(81, 'phone', 't235_usb_exec_ram_runs_probe_and_returns', 'FAIL',
 'phone: home -Select-> seam.usb (task 79, T-usb-79a)',
 '[phone @master] Uploading a Thumb probe via UPLOAD_RAM and invoking EXEC_RAM flushes caches, runs the probe in PSRAM, appends a custom LOG_TEL record, and returns its 32-bit result over USB (targets: phone) -- RED until task 79 (usb_exec_ram)',
 'LUGHOS_TARGET=phone tests/emu/run_one.py t235_usb_exec_ram_runs_probe_and_returns',
 '2026-11-01', '2026-11-01'),

(82, 'phone', 't236_usb_exec_ram_calls_mncall_while_radio_up', 'FAIL',
 'phone: home -Select-> seam.usb (task 79, T-usb-79b)',
 '[phone @master] An uploaded RAM probe calls MNCALL_StartCallEx through the live API table over USB without flashing or rebooting, and the call log records the dial (targets: phone) -- RED until task 79 (usb_exec_ram)',
 'LUGHOS_TARGET=phone tests/emu/run_one.py t236_usb_exec_ram_calls_mncall_while_radio_up',
 '2026-11-01', '2026-11-01'),

(83, 'phone', 't237_usb_exec_ram_rejects_bad_magic_or_oversize', 'FAIL',
 'phone: home -Select-> seam.usb (task 79, T-usb-79c)',
 '[design @master] UPLOAD_RAM/EXEC_RAM rejects payloads without a valid header magic or larger than the PSRAM scratch arena and leaves the phone running normally (targets: design,phone) -- RED until task 79 (usb_exec_ram)',
 'LUGHOS_TARGET=phone tests/emu/run_one.py t237_usb_exec_ram_rejects_bad_magic_or_oversize',
 '2026-11-01', '2026-11-01'),

(84, 'phone', 't238_usb_live_session_survives_call_lifecycle', 'FAIL',
 'phone: any -incoming-call-> incall.incoming -Select-> incall -End-> home (task 80, T-usb-80a)',
 '[phone @master] Across a full call lifecycle (ring -> answer -> EXEC_RAM probe -> hangup -> standby), USB LOG_DRAIN and PEEK remain responsive with zero resets (targets: phone) -- RED until task 80 (usb_live_session)',
 'LUGHOS_TARGET=phone tests/emu/run_one.py t238_usb_live_session_survives_call_lifecycle',
 '2026-11-01', '2026-11-01'),

(85, 'phone', 't239_usb_live_session_survives_sms_send_receive', 'FAIL',
 'phone: home -Select-> seam.usb (task 80, T-usb-80b)',
 '[phone @master] Across a live MT SMS arrival (0xa1a7) and an MO SMS send triggered via EXEC_RAM, USB LOG_DRAIN captures both events in one session without a reboot (targets: phone) -- RED until task 80 (usb_live_session)',
 'LUGHOS_TARGET=phone tests/emu/run_one.py t239_usb_live_session_survives_sms_send_receive',
 '2026-11-01', '2026-11-01');

COMMIT;
```

---

## 6. Operator Instructions (Foreman / Worker Protocol)

### Step 1: Insert Tracker Rows
Run the SQL block in Section 5 against `/Users/conornash/Documents/lughos/tracker.db` from the main checkout, then verify with:
```sh
sqlite3 -readonly /Users/conornash/Documents/lughos/tracker.db "SELECT id, app, state, title FROM task WHERE id BETWEEN 76 AND 80;"
sqlite3 -readonly /Users/conornash/Documents/lughos/tracker.db "SELECT id, app, state, title FROM test WHERE id BETWEEN 76 AND 85;"
```

### Step 2: Execute Task 76 First (Read-Only Investigation)
- Spawn a `Research` or `Scoped` worker on `/Users/conornash/Documents/nokia105/work/map/kern.bin` and `/Users/conornash/Documents/nokia105/spreadtrum_flash/custom_fdl/` to write `/Users/conornash/Documents/lughos/work/reports/inv-usb-live.md`.
- Determine whether:
  1. Calling the stock `UCOM` initializer in `kern.bin` brings up USB `1782:3d00` endpoints cleanly from `P_APP`, **or**
  2. Driving the SC6531E USB device block (`0x20300000`, using the endpoint setup already proven on silicon in `nor_fdl1` / `fpdoom`) from our periodic tick on `P_APP` is simpler and independent of stock `DIAG` framing.
- Update task `76` to `emu-verified` once `work/reports/inv-usb-live.md` cites exact addresses and the chosen transport.

### Step 3: Enforce the Design → Red → Green → Mutation Gate (`tools/audit.ysh`)
For Tasks **77, 78, 79, 80**, every branch must pass `tools/audit.ysh BASE..HEAD` and `tools/integrate.ysh`:

1. **Design Commit (`docs/flows/phone.d2`):**
   - Add the `seam.usb` node to `docs/flows/phone.d2`.
   - Add the `# T-usb-77a`, `# T-usb-77b`, `# T-usb-78a`, `# T-usb-78b`, `# T-usb-78c`, `# T-usb-79a`, `# T-usb-79b`, `# T-usb-79c`, `# T-usb-80a`, and `# T-usb-80b` comment lines in `docs/flows/phone.d2` (`tests/emu/lib.py:validate_case` and `tools/ctxpack.ysh` require both `seam.usb` and `# T-usb-*` to exist in `docs/flows/phone.d2`).
   - Re-render `docs/flows/phone.svg` if required by the flow check.
   - Update the task's `state` to `'d2-drawn'`.

2. **RED Commit (`tests/emu/cases/t230_*.py` .. `t239_*.py`):**
   - Write each test case file with `EXPECT = "red"` and docstring starting `"RED until task <N> ..."`.
   - Run `uv run --with pillow python3 tests/emu/run_all.py --update-expected` so `tests/emu/expected_cases.txt` lists the new tests (`check_expected_cases` fails with exit 2 `BROKEN` otherwise).
   - Run each new test with `tests/emu/run_one.py`, save the failing log under `work/reports/usb-live/`, and include `RED`, the `T-usb-*` IDs, and `FAIL work/reports/usb-live/<name>.fail.txt` in the commit message.

3. **GREEN Commit (`nokia105-emu`, `nokia105/lugh/shell/`, `lughos/src/`):**
   - Implement the code, flip `EXPECT = "green"`, remove `"RED until ..."` from the case docstrings, run `run_one.py` on both `design` and `phone` targets, and include `GREEN`, the `T-usb-*` IDs, and `PASS work/reports/usb-live/<name>.pass.txt` in the commit message.

4. **Mutation Proof (`work/reports/mutation-usb-live.md`):**
   - For each test `t230`..`t239`, introduce a 1-line code mutant in the responder/loader (for example, skipping the cache flush, dropping the `LOG_DRAIN` advance, or corrupting the `PONG` magic), record the `FAIL` log, restore the code, and record the `PASS` log in `work/reports/mutation-usb-live.md`.

5. **Integration Gate (`tools/integrate.ysh`):**
   - Run `tools/integrate.ysh` from the `int` worktree.
   - Only after `VERIFY PASS` and `0` unexpected `FAIL`/`BROKEN` on both `design` and `phone` suites, update tasks `77`–`79` to `'in-image'` and write the new image path to `/Users/conornash/Documents/lughos/work/hitl/latest-image.txt`.

### Step 4: Briefing Rule (`docs/handset/DELEGATION.md`)
When briefing workers for Task 79 (`UPLOAD_RAM` / `EXEC_RAM`):
- State the setting in line 1: *"Our own Nokia 105 handset, our own dump, and our own LughOS shell running on `P_APP`."*
- Describe Task 79 as *"allocating a scratch buffer in PSRAM for our USB test runner and calling a function pointer with cache maintenance"*, not as *"code injection"* or *"arbitrary payload execution"*.
