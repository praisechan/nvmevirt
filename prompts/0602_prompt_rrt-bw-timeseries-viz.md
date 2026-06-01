# Prompt — Visualize Read-Bandwidth-vs-Time Across a Read-Reclaim (RRT) Sweep

> Paste this into a fresh OMC session at `/home/juchanlee/nvmevirt`. Goal: **plot read bandwidth as a
> function of time** (x-axis = elapsed seconds, y-axis = read MiB/s) for several
> `READ_RECLAIM_THRESHOLD` (RRT) values plus the reclaim-OFF baseline, on the 16 GiB emulated device —
> so the **bandwidth fluctuation (reclaim-induced dips) is visible over time** and the effect of RRT on
> dip *frequency* is obvious at a glance. Read-reclaim is already implemented, committed, and was
> swept once (numbers only) in `reports/0601_report_rrt-sweep-16g.md`; this experiment turns that into
> **time-series figures**.

---

## 0. Read first (context — do not skip)
- `reports/0601_report_rrt-sweep-16g.md` — the RRT sweep this builds on (geometry, per-RRT avg/min BW, reclaim counts). **Start here.**
- `QUICKSTART.md` — turnkey build/load/run + device-identity safety (note: device is now **16 GiB**, pinned by **load-order**, NOT size).
- `prompts/experiment.md` — original staged runbook (coexistence, build/load, fio). **Authoritative** where docs disagree.
- `prompts/0601_prompt_larger-device-rrt-sweep.md` — the previous experiment's full instructions (device-pin §2.3, geometry §3, methodology §4).
- `rr_run.sh`, `rr_sweep.sh` — the existing one-shot runner + sweep driver (reuse them).

## ⚠️ CRITICAL — shared machine (read every time)
- `/dev/nvme0n1` = **REAL disk** (Crucial `CT2000P3SSD8`, 1.8 TB). **Never write to it.**
- Another user runs **stock `nvmev`** @ `memmap=16G$96G` (their device ≈15 GiB, model `CSL_Virt_MN_01`). **Never `rmmod` it, never point fio at it, never run stock `reload.sh`.**
- **OURS** = module **`nvmev_rr`**, `/proc/nvmev_rr`, region **`memmap=16G$64G`** (`memmap_start=68719476736`), device ≈15 GiB.
- ‼️ **Our device (~15 GiB) is the SAME size as the other user's (~15 GiB) and shares model `CSL_Virt_MN_01`. DO NOT pin by size.** Pin by **LOAD-ORDER**: snapshot `/dev/nvme*n1` after `rmmod nvmev_rr` and before `insmod`; the newly-appeared node is ours, cross-checked by `/proc/nvmev_rr` present + `memmap_start=68719476736`, refusing `/dev/nvme0n1` and ambiguous/empty matches. `rr_run.sh` **already does this** — reuse it; never hand-roll a size check.

## ✅ No reboot needed this time
The device is **already 16 GiB** (region `memmap=16G$64G` is in `/proc/cmdline` and persists across reboots). This experiment changes **only RRT + plotting** — **no grub edit, no reboot, no hard gate.** (Confirm with `grep -o 'memmap=[^ ]*' /proc/cmdline` → expect `16G$96G` and `16G$64G`.)

## Standing instructions (the user requested these)
- **Slack the user** via `./notify.sh "<msg>"` (HTTP 200 = delivered) at each milestone and for any user-only/ambiguous decision (e.g. installing packages with sudo, picking RRT values), then **pause** — also surface it in-session. Don't silently guess.
- **sudo:** the `/etc/sudoers.d/nvmev-timeout` drop-in (120 min, `!tty_tickets`) is installed, but tickets still lapse between turns. Batch sudo work into one shell right after the user primes `sudo -v`; if `sudo -n true` fails, Slack the user to run `! sudo -v` and wait.
- **Verify with evidence** (dmesg reclaim counts + the actual bw logs) before claiming a result.

---

## 1. ⚠️ NEW environment hazard — plotting toolchain is MISSING (resolve before plotting)
Verified 2026-06-01 on this host: **`matplotlib`, `pandas`, `gnuplot`, and even `pip` are NOT installed.** Only **`python3` 3.10.12 + `numpy` 1.21.5** are present. So you cannot `import matplotlib` or `pip install` out of the box. Pick a path:

- **Path A (nicer PNG, needs sudo + network):** `sudo apt install -y python3-matplotlib` (pulls matplotlib for the system python3). **This is a user-only/ambiguous step → Slack the user first** ("OK to `apt install python3-matplotlib`? needs sudo + network"), pause, then install once sudo is primed. (`gnuplot` via `sudo apt install -y gnuplot` is an alternative.)
- **Path B (zero-dependency, ALWAYS works — make this the guaranteed deliverable):** write a small pure-Python script that reads the bw logs and emits a **self-contained SVG** line chart (compute geometry by hand; numpy is available for min/avg/max but stdlib is enough). No external libs, no display, no install. Produce this **regardless**, so the figure never blocks on a package install.

Recommended: always produce the **SVG (Path B)**; additionally produce a **PNG (Path A)** if the user approves the install. Headless either way — save to files, never try to open a window.

---

## 2. Data — reuse or re-run
The bw logs from the 0601 sweep already exist and are sufficient for a first figure:
```
rr_results/bw_baseline_off_16g.log   # RRT=1e9 (reclaim OFF)
rr_results/bw_rrt16_16g.log          # RRT=16
rr_results/bw_rrt64_16g.log          # RRT=64
rr_results/bw_rrt256_16g.log         # RRT=256
rr_results/bw_rrt1024_16g.log        # RRT=1024
```
**Bw-log format (fio `write_bw_log`, `log_avg_msec`):** comma-separated, `col1 = time_ms`, `col2 = bandwidth_KiB/s`, rest unused. So **time_s = col1/1000**, **MiB/s = col2/1024**. Existing logs are at **1 s** resolution (≈179–180 points over 180 s).

- **Fast path:** just plot the existing logs above (§4) — good enough to see the dips.
- **Better path (recommended):** **re-run the sweep with finer time resolution** so bursts are smooth, not aliased. fio's `rr-seq-read.fio` hardcodes `log_avg_msec=1000`; make it env-overridable (e.g. `LOG_AVG_MSEC`, default 1000) or add a `rr-seq-read-fine.fio` variant, then re-run with **`log_avg_msec=200`–`500`**. Keep everything else identical to 0601: `RR_SIZE=512m`, `RR_RUNTIME=180`, `set_perf_rr.py max`, prep-write before each read loop, device pinned by load-order. Reuse `rr_sweep.sh` (optionally add finer points like RRT ∈ {8, 16, 32, 64, 128, 256, 1024} for a denser family of curves — say so in the report).

Whichever you choose, **restore `READ_RECLAIM_THRESHOLD` to 8 and leave the module in a known state at the end** (as 0601 did). The module is currently loaded at RRT=8 on the ~15 GiB node.

---

## 3. Methodology (if re-running)
- Same workload as 0601 so curves are comparable: `RR_SIZE=512m`, `RR_RUNTIME=180`, `set_perf_rr.py max`, prep-write each time.
- Per-pass `read_cnt` increment = `flashpgs_per_blk = 4` at this geometry (block = 128 KiB = 4 flash pages), so a block crosses RRT after `RRT/4` passes; pass rate ≈1.87/s. Expect: low RRT → frequent dips (near-continuous at 16), high RRT → rare/single late dip (RRT=1024 fired once at ~t=136 s in 0601).
- Capture reclaim counts from dmesg (`total_reclaims=` cumulative counter — authoritative; line-count wraps) to annotate each curve.

---

## 4. Plot specification (the deliverable figures)
Produce **both** of these from the bw logs (time on x, read MiB/s on y):

1. **Overlay plot** — all RRT curves + the OFF baseline on one set of axes.
   - x-axis: elapsed time (s, 0→180). y-axis: read bandwidth (MiB/s), start y at 0 (or a clearly-labeled break ~600–1000 to make dips legible — your call, but label it).
   - one line per series, distinct colors, **legend** labeling each by RRT (and its reclaim count, e.g. "RRT=64 (5,120 reclaims/inst)").
   - draw the baseline (~964 MiB/s flat) as a reference line.
   - title, axis labels with units, grid.

2. **Small-multiples** — one stacked panel per RRT (shared x and y axes), baseline on top. This is usually the clearest way to see that **lower RRT = more frequent dips** (not deeper). Annotate each panel with its avg BW and dip count.

Output: save to `reports/figures/` (create it) as **`rrt_bw_timeseries_overlay.svg`** and **`rrt_bw_timeseries_panels.svg`** (+ `.png` versions if matplotlib was installed). Keep a tiny, committed generator script (e.g. `plot_rrt_bw.py`) so the figures are reproducible from the logs.

**Sanity-check the rendered figure** before claiming done: open/inspect the SVG/PNG (or re-read the numeric min/avg/max per series and confirm the plotted curves match — e.g. baseline flat ~964, RRT=16 sawtooth floor ~660, RRT=1024 single dip near t=136 s). Don't ship a plot you haven't verified renders and matches the data.

---

## 5. Deliverable
- The two figures (SVG always; PNG if matplotlib installed) under `reports/figures/`.
- The generator script (`plot_rrt_bw.py`) committed alongside.
- A short markdown note **`reports/0602_report_bw-timeseries-viz.md`**: what was plotted, data source (reused 0601 logs vs fresh fine-grained run + the log_avg_msec used), how to regenerate (`python3 plot_rrt_bw.py`), and 2–3 sentences reading the figures (dip frequency vs RRT, common dip floor, baseline flatness). Embed/reference the figures. Keep it short — the analysis already lives in 0601.

---

## 6. Order of work (Slack at boundaries)
1. Preflight: read §0 docs; confirm coexistence state (`/proc/cmdline` has both regions, `lsblk`, `lsmod`, `/proc/nvmev_rr`, sudo). No reboot needed.
2. Decide data source: **fast** (plot existing `rr_results/bw_*_16g.log`) or **better** (re-run sweep with `log_avg_msec`≈250). If re-running, reuse `rr_run.sh`/`rr_sweep.sh` (load-order device pin), verify each point fires.
3. Resolve the plotting toolchain (§1): always build the **zero-dep SVG**; **Slack the user** before any `apt install` for the optional PNG.
4. Generate both figures (§4), **verify they render and match the numbers**, write `reports/0602_report_bw-timeseries-viz.md`.
5. Restore `READ_RECLAIM_THRESHOLD=8`, leave module in a known state. Final Slack summary.
6. Commit only if the user asks; if so, branch off (current branch: `read-reclaim-16g-rrt-sweep`) — do not push without asking.

> Safety reminders, condensed: never `nvme0n1` (real) or the other user's `nvmev`; pin our node by **load-order** + `/proc/nvmev_rr` + `memmap_start=64G` (NEVER by size — both devices are ~15 GiB); no reboot/grub change this experiment; `sudo -v` per batch (Slack the user); no `KCFLAGS` on kernel 6.8; matplotlib/pip are NOT installed — default to the dependency-free SVG generator.
