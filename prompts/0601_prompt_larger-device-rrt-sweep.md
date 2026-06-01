# Prompt — Read-Reclaim on a Larger Device (16 GB) with a Read-Count (RRT) Sweep

> Paste this into a fresh OMC session at `/home/juchanlee/nvmevirt`. Goal: re-run the **block-level
> read-reclaim** experiment on a **larger emulated device (≈16 GiB)** so NAND blocks hold many more
> pages (closer to real hardware), **sweep several `READ_RECLAIM_THRESHOLD` (RRT) values**, and
> produce **one consolidated experiment report** in `reports/`. Read-reclaim is already implemented
> and committed (branch `read-reclaim`); this experiment characterizes it across device size and RRT.

---

## 0. Read first (context — do not skip)
- `prompts/experiment.md` — the original staged runbook (coexistence plan, build/load, fio workloads). **Authoritative** where docs disagree.
- `reports/0531_report_read-reclaim.md` — what was implemented and the small-device (4 GiB) results. **Start here** to see the code changes and the geometry discussion.
- `reports/read_reclaim_research.md` — design rationale (§6 = code hooks; RRT is "a tunable, not a law of physics").
- `QUICKSTART.md` — turnkey build/load/run, known-good settings, device-identity safety.

## ⚠️ CRITICAL — shared machine (read every time)
- `/dev/nvme0n1` = **REAL disk** (Crucial `CT2000P3SSD8`, 1.8 TB). **Never write to it.**
- Another user runs **stock `nvmev`** @ `memmap=16G$96G` (their device ≈ 15 GiB, model `CSL_Virt_MN_01`). **Never `rmmod` it, never point fio at it, never run stock `reload.sh`.**
- **OURS** = renamed module **`nvmev_rr`** with **`/proc/nvmev_rr`**, in **our own reserved region**. Identify ours and ONLY operate on ours.
- ‼️ **NEW HAZARD this experiment introduces:** at 16 GiB our device's *logical size* (~15 GiB) will be **about the same as the other user's** (~15 GiB). The model string `CSL_Virt_MN_01` is shared by both modules. **So size + model can no longer tell our device apart from theirs.** You MUST pin our node another way (see §2.3) — getting this wrong risks pointing fio at the other user's device.

## Standing instructions (the user requested these)
- **Slack the user for any user-only action or decision** via `./notify.sh "<what you need + options>"` (HTTP 200 = delivered) **and** surface it in-session, then **pause** — e.g. the grub+reboot, picking values, any destructive/ambiguous step. Do not silently guess.
- **sudo:** agent shells use a different tty than the user's `sudo -v`; even with the 120-min drop-in it lapses between turns. Batch all sudo work into one shell right after the user primes `sudo -v`; if it has lapsed, Slack the user to run `! sudo -v` and wait.
- **Verify with evidence** (dmesg + bandwidth) before claiming any result. Notify at each milestone.

---

## 1. One hard gate — enlarge our reserved region to 16 GiB (USER ONLY: grub + reboot)
Our current region is `memmap=4G$64G`. To get a 16 GiB device, reserve **`memmap=16G$64G`** (16 GiB at the 64 GiB offset = 64–80 GiB; does NOT overlap the other user's 96–112 GiB). Keep theirs.

Target `/etc/default/grub` line (mirror the existing `\$` escaping):
```
GRUB_CMDLINE_LINUX="memmap=16G\$96G memmap=16G\$64G"
```
This is a **coordinated reboot** (drops the other user's device until they re-`insmod`; no grub change needed on their side). **Slack the user, ask them to warn the other user, then edit grub + `sudo update-grub` + `sudo reboot` themselves.** After reboot, confirm BOTH:
`grep -o 'memmap=[^ ]*' /proc/cmdline` → must show `memmap=16G$96G` **and** `memmap=16G$64G`.
(If `insmod` later panics in `__pci_enable_msix()`/`nvme_hwmon_init()`, add `intremap=off` + reboot.)

> If the user prefers an even larger device for truly "hundreds of pages/block", they can choose a bigger region (e.g. `64G$64G` if free) — confirm the offset doesn't overlap 96–112 GiB and there's enough RAM (host has 125 GiB). Default to 16 GiB unless told otherwise.

---

## 2. Update the tooling for the larger device (agent — before any run)

### 2.1 `READ_RECLAIM_THRESHOLD` location
`ssd_config.h`, `#ifndef`-guarded:
```c
#ifndef READ_RECLAIM_THRESHOLD
#define READ_RECLAIM_THRESHOLD (8)
#endif
```
You will edit this value per sweep point (rebuild picks it up). Restore to a sensible default at the end.

### 2.2 `rr_run.sh` — fix the memmap size (currently hardcoded 4G)
`rr_run.sh` calls `MEMMAP_START=64G MEMMAP_SIZE=4G CPUS=22,23 ./reload.sh`. **Change `MEMMAP_SIZE=4G` → `MEMMAP_SIZE=16G`** (or parameterize via an env var). `reload.sh` defaults are also 4G — pass `MEMMAP_SIZE=16G` explicitly.

### 2.3 `rr_run.sh` — REPLACE the size-based device pin (it will mis-fire at 16 GiB)
The current guard pins our node by `size == 3827 MiB`. At 16 GiB that is wrong AND collides with the other user's ~15 GiB device. Replace it with a **load-order** identification that cannot confuse the two modules:
```bash
# BEFORE insmod (after rmmod nvmev_rr): snapshot existing nvme namespaces
before=$(ls /dev/nvme*n1 2>/dev/null | sort)
# ... reload.sh (rmmod nvmev_rr -> insmod nvmev_rr) ...
after=$(ls /dev/nvme*n1 2>/dev/null | sort)
DEV=$(comm -13 <(echo "$before") <(echo "$after"))   # the newly-appeared node = ours
# Hard cross-checks (ALL must hold):
[ -d /proc/nvmev_rr ] || { echo "ABORT: nvmev_rr not loaded"; exit 1; }
[ "$(cat /sys/module/nvmev_rr/parameters/memmap_start)" = "68719476736" ] || { echo "ABORT: not our 64G region"; exit 1; }
[ -n "$DEV" ] && [ "$DEV" != "/dev/nvme0n1" ] || { echo "ABORT: refusing real disk / empty DEV"; exit 1; }
```
(Backup: parse our load's dmesg — the last `nvme nvmeX: pci function ...` line right after our insmod is our controller → `/dev/nvmeXn1`.) **Never** fall back to `nvme0n1` (real disk). If the diff is ambiguous (e.g. other user loaded at the same instant), STOP and Slack the user.

### 2.4 `set_perf_rr.py`
Use `nvmev-evaluation/common/set_perf_rr.py max` (targets `/proc/nvmev_rr`; the stock `set_perf.py` writes `/proc/nvmev` and silently no-ops for our module).

---

## 3. Confirm the new geometry (record it for the report)
After the first successful load, read the geometry NVMeVirt prints (needs sudo):
```bash
sudo dmesg | grep -E 'Total Capacity|Init FTL|blk-size|line-size'
```
Record per-instance: `pgs_per_blk`, `flashpgs_per_blk`, `pgs_per_line`, `tt_lines`, block size, line size.
Expected at 16 GiB (4 partitions, `BLKS_PER_PLN=8192`, `FLASH_PAGE_SIZE=32K`, `pgsz=4K`): per-instance capacity ≈ 4 GiB → `blk_size ≈ 128 KiB` → **`pgs_per_blk ≈ 32`, `flashpgs_per_blk ≈ 4`** (vs 8 / 1 on the old 4 GiB device). Verify against dmesg; if you chose a bigger region, recompute. **This is the point of the larger device: blocks now span several flash pages, so `read_cnt` increments several times per block per sequential pass.**

---

## 4. Methodology — RRT sweep (the "different read count" runs)

Per-pass increment of a block's `read_cnt` ≈ `flashpgs_per_blk` (one sense per flash page; a full sequential pass over a block issues that many senses). Use this to size RRT so reclaim fires several times within the run.

1. **Baseline / control (reclaim OFF):** set `READ_RECLAIM_THRESHOLD` to `1000000000`, run → expect **0 reclaims, flat bandwidth**. This is the reference.
2. **Sweep (reclaim ON):** run the SAME workload at several thresholds, e.g. **RRT ∈ {16, 64, 256, 1024}** (adjust to the measured geometry/pass rate — lower = more frequent reclaim = larger BW hit). For each value: edit `ssd_config.h`, then `./rr_run.sh rrt<value> <runtime>`.
   - Suggested run: `RR_SIZE=2g`, `RR_RUNTIME=180`. If a high RRT never fires within 180 s, either lower it, raise `RR_RUNTIME`, or shrink `RR_SIZE` (fewer blocks re-read faster) — and **say so in the report** (don't silently let a point not fire).
3. Keep `MEMMAP_SIZE=16G`, `set_perf_rr.py max`, and the prep-write before every read loop (reads only count on blocks holding valid data).

`rr_run.sh` already: rmmods → reloads (picks up current RRT) → pins our device (§2.3) → `dmesg -C` → set_perf → prep-write → looped seq-read → reports reclaim count + saves `rr_results/{fio_<label>.log, bw_<label>.log}`.

### Metrics to capture per run (for the report table)
- `READ_RECLAIM_THRESHOLD`
- read-reclaim event count (`total_reclaims` from dmesg; note dmesg ring-buffer wrap — the counter is authoritative)
- sustained read BW: **avg, min (dip), max** (from `rr_results/bw_<label>.log`)
- dip depth vs the reclaim-OFF baseline (%)
- write amplification proxy: reclaim count × `pgs_per_line` (relocated pages) and × `blks_per_line` (erases); if you add `/proc/nvmev_rr` counters for page-copies/erases, prefer those
- (optional) read latency mean/p99 if you extend fio output
- fio error count (must be 0)

---

## 5. Deliverable — ONE consolidated report

Write a single markdown report: **`reports/0601_report_rrt-sweep-16g.md`** containing:
1. **Summary** — what was tested (16 GiB device + RRT sweep) and the headline finding.
2. **Setup** — region change (`memmap=16G$64G`), module `nvmev_rr`, CPUS, RR_SIZE/RR_RUNTIME, `set_perf_rr max`, exact device node used and HOW it was identified (§2.3).
3. **Geometry** — the dmesg-reported per-instance numbers (block/line size, `pgs_per_blk`, `flashpgs_per_blk`), and contrast with the 4 GiB device from `reports/0531_report_read-reclaim.md` (8 pages/block → many pages/block).
4. **Methodology** — the sweep values and why, per-pass increment reasoning, baseline control.
5. **Results** — a table: RRT | reclaim events | avg BW | min(dip) BW | dip % vs baseline | (WA proxy) | errors. Plus 1–2 representative per-second BW excerpts showing dips.
6. **Analysis** — RRT vs reclaim frequency vs bandwidth/WA tradeoff; how the larger block (more flash pages/block) changes the per-pass increment and thus the effective RRT scaling; compare to the small-device run.
7. **Conclusions + follow-ups** — recommended RRT range for this geometry; ideas (Option B per-block reclaim, WL-level/STRAW, latency metrics, even larger device).

Keep it evidence-backed (cite the dmesg counters and `bw_*.log` numbers). One report, not per-run files (raw logs stay in `rr_results/`).

---

## 6. Order of work (gate each on the previous; Slack at boundaries)
1. Preflight + read §0 docs. Confirm coexistence state (`lsblk`, `/proc/cmdline`, `lsmod`).
2. **Slack the user** for the §1 grub change + reboot to `memmap=16G$64G`; pause until confirmed.
3. Update `rr_run.sh` (§2.2 size, §2.3 device pin). Build only (`make`) to confirm clean.
4. First load + **verify our device** (§2.3) + record geometry (§3). Run the **reclaim-OFF baseline**.
5. Run the **RRT sweep** (§4), verifying each point actually fires (adjust if not).
6. Write **`reports/0601_report_rrt-sweep-16g.md`** (§5). Restore `READ_RECLAIM_THRESHOLD` to a sensible default and leave the module in a known state.
7. Final Slack summary. (Commit only if the user asks; if so, branch off — do not push without asking.)

> Safety reminders, condensed: never `nvme0n1` (real) or the other user's `nvmev`; pin our node by load-order + `/proc/nvmev_rr` + `memmap_start=64G` (NOT by size at 16 GiB); `sudo -v` per batch (Slack the user); no `KCFLAGS` on kernel 6.8.
