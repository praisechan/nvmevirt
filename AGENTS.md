# AGENTS.md — fundamental rules for working in this repo

This is a fork of NVMeVirt used for **read-reclaim / read-disturb experiments** on a **shared lab
machine**. These are the durable, non-negotiable rules. They override convenience. When a detailed
runbook is needed, defer to the canonical docs in §6 — `prompts/experiment.md` is authoritative where
docs disagree.

---

## 1. ⚠️ Safety first — this is a SHARED machine (read every session)
Another user runs their own NVMeVirt on the same host. Getting device identity wrong can destroy their
device or corrupt the real disk.

- `/dev/nvme0n1` = **REAL disk** (Crucial `CT2000P3SSD8`, 1.8 TB). **Never write to it.**
- Other user's **stock `nvmev`** @ `memmap=16G$96G` (~15 GiB, model `CSL_Virt_MN_01`).
  **Never `rmmod nvmev`, never point fio at it, never run stock `reload.sh`** (its `rmmod` would hit theirs).
- **OURS** = renamed module **`nvmev_rr`** with **`/proc/nvmev_rr`**, region **`memmap=16G$64G`**
  (`/sys/module/nvmev_rr/parameters/memmap_start = 68719476736`). Only ever operate on ours.
- **Pin our device by LOAD-ORDER, never by size.** Our device (~15 GiB) is now the same size as the
  other user's and shares the model string — size/model can't disambiguate. Snapshot `/dev/nvme*n1`
  after `rmmod nvmev_rr` and before `insmod`; the **newly-appeared node is ours**, cross-checked by
  `/proc/nvmev_rr` present + `memmap_start=64G`, refusing `/dev/nvme0n1` and empty/ambiguous matches.
  `rr_run.sh` already does this — reuse it; never hand-roll a size check.
- Unload **ours only**: `sudo rmmod nvmev_rr`.

## 2. Directory & file conventions
- **`prompts/`** — experiment prompts, one per experiment, named **`MMDD_prompt_<slug>.md`** (e.g.
  `0601_prompt_larger-device-rrt-sweep.md`). Each prompt is **self-contained for a fresh session**:
  context-to-read, the §1 safety rules, methodology, deliverable, and an ordered work plan.
  `prompts/experiment.md` is the original staged runbook and is **authoritative** where docs disagree.
- **`reports/`** — write a report **only after an experiment has succeeded and is verified**, named
  **`MMDD_report_<slug>.md`**. One **consolidated, evidence-backed** report per experiment (not
  per-run files): summary, setup, geometry, methodology, results table, analysis, conclusions. **Cite
  the evidence** (dmesg counters, `rr_results/*.log` numbers). Design/background notes also live here
  (e.g. `read_reclaim_research.md`).
- **`rr_results/`** — raw fio/bandwidth logs (`fio_<label>.log`, `bw_<label>.log`). The evidence behind
  reports; reports reference these rather than inlining everything.
- **`QUICKSTART.md`** — the durable turnkey build/load/run guide. **Keep it current**: when you find a
  new setup step, quirk, fix, or known-good setting, record it with the exact command. Correct/delete
  stale content rather than appending contradictions.
- Helper scripts live at repo root: `reload.sh`, `rr_run.sh`, `rr_sweep.sh`, `notify.sh`. Prefer
  extending/reusing them over re-deriving their logic inline.

## 3. Build, load, run
- Kernel **6.8.0-111**: build with **gcc-12** and **no `KCFLAGS`** (the `-DE820_TYPE_RESERVED_KERN`
  define is for the old 6.5 kernel and breaks the 6.8 build). Secure Boot is **off** — no module signing.
- Load via `reload.sh` with explicit params: `MEMMAP_START=64G MEMMAP_SIZE=16G CPUS=22,23 ./reload.sh`.
  Healthy load: dmesg shows the device created + `/proc/nvmev_rr/` appears + a new node from §1.
- Use **`set_perf_rr.py`** (targets `/proc/nvmev_rr`), **not** stock `set_perf.py` (writes `/proc/nvmev`
  → silently no-ops for our module).
- **Reads only count on written LPNs** — conv_ftl skips unwritten LPNs (no NAND sense). Always
  **prep-write the region before any read** measurement.
- The reclaim workload (`rr-seq-read.fio`) is intentionally **QD1/psync** so reclaim dips are visible —
  that is not a bandwidth bug; peak-BW measurement needs a separate high-concurrency job.

## 4. sudo & notifications (the user requires these)
- **sudo across ttys:** agent shells use a different tty than the user's `sudo -v`; the
  `/etc/sudoers.d/nvmev-timeout` drop-in helps but tickets still lapse between turns. Batch sudo work
  into one shell right after the user primes sudo. If `sudo -n true` fails, **ask the user to run
  `! sudo -v`** and wait.
- **Slack the user** via `./notify.sh "<msg>"` (HTTP 200 = delivered) at **every milestone** and for
  **any user-only or ambiguous decision** — grub/reboot, picking values, package installs, any
  destructive/hard-to-reverse step. After asking, **surface it in-session and pause** — never silently
  guess.
- **User-only actions** (agent cannot do): grub edits, reboots, anything needing the user's terminal.

## 5. Process
- **Read the context docs first** (§6); broad tasks → explore, then plan.
- **Gate each stage on the previous** and Slack at boundaries.
- **Verify with evidence before claiming success** — dmesg + bandwidth numbers, not assumptions.
  Authoring and verification are separate passes.
- **Leave a known state:** at the end of an experiment, restore tunables to a sensible default (e.g.
  `READ_RECLAIM_THRESHOLD`) and leave the module loaded/known. Say so in the report.
- **Git:** commit **only when the user asks**. Don't commit to the default branch (`main`); use the
  working feature branch (currently `read-reclaim`). **Never push without asking.** Don't commit
  transient run artifacts (e.g. `nvmev-evaluation/fio/rr-read_bw.*.log`); raw evidence in `rr_results/`
  may be committed.

## 6. Canonical docs (read before acting)
| File | Purpose |
|---|---|
| `prompts/experiment.md` | Original staged runbook — **authoritative** where docs disagree |
| `QUICKSTART.md` | Turnkey build/load/run + known-good settings + device-identity safety |
| `reports/read_reclaim_research.md` | Read-reclaim design rationale (§6 = code hooks) |
| `reports/MMDD_report_*.md` | What prior experiments did + their verified results |
| `prompts/MMDD_prompt_*.md` | Per-experiment instructions |
