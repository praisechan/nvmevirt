# NVMeVirt Read-Reclaim — Experiment & Build Runbook

> Working context/prompt for implementing and verifying **block-level read reclaim** in NVMeVirt.
> Design rationale: `read_reclaim_research.md`. Implementation target: **physical NAND block
> granularity** (counter + reclaim per `nand_block`).
>
> **Order of work (as directed):**
> 1. **Verify baseline NVMeVirt runs well** (FIO sequential read) — *before* touching reclaim code.
> 2. **Implement read reclaim** (physical-block granularity).
> 3. **Verify the implementation**: a long sequential-read run should show **read-bandwidth drops**
>    when reclaim fires. Use a **low read-count threshold** so reclaim triggers fast and often.

---

## 0. START HERE — status & preflight (fresh session, no prior chat context)

**Host:** Linux `6.5.0-35-generic`, 125 GiB RAM, 24 cores. Working dir `/home/juchanlee/nvmevirt`.

**Already done in the previous session (do NOT redo):**
- `Kbuild` set to `CONFIG_NVMEVIRT_SSD := y` (builds `conv_ftl.c`, base SAMSUNG_970PRO).
- `reload.sh` created (build+load one-shot, KCFLAGS + memmap defaults baked in).
- FIO workloads created: `nvmev-evaluation/fio/workloads/rr-prep-write.fio`, `rr-seq-read.fio`.
- `read_reclaim_research.md` written (design + code hooks in its §6).
- User ran `sudo -v` (sudo cache primed — but default TTL is 15 min unless the §1d drop-in was
  installed, so it may have expired; if any `sudo` prompts, ask the user to run `! sudo -v` again).
- **Read-reclaim code is NOT yet implemented.**

### Preflight checks — run these first; fix any that fail before proceeding
```bash
cd /home/juchanlee/nvmevirt
echo "--- gcc-12 (build needs it) ---";   gcc-12 --version 2>/dev/null | head -1 || echo "MISSING -> sudo apt install -y gcc-12"
echo "--- fio (verification needs it) ---"; fio --version 2>/dev/null || echo "MISSING -> sudo apt install -y fio"
echo "--- memmap reservation ---";          grep -o 'memmap=[^ ]*' /proc/cmdline || echo "MISSING -> grub step §1b + REBOOT (user only)"
echo "--- sudo cache ---";                  sudo -n true 2>/dev/null && echo "cached" || echo "expired -> ask user: ! sudo -v"
echo "--- module loaded? ---";              lsmod | grep -q '^nvmev' && echo "loaded" || echo "not loaded"
```

**Who can fix what:**
- `gcc-12` / `fio` missing → `sudo apt install -y gcc-12 fio` (the agent may run this *if* the sudo
  cache is valid; otherwise ask the user).
- **memmap missing → user only** (grub edit + **reboot**, §1b). The agent cannot reboot. This is the
  one true hard gate; if `/proc/cmdline` has no `memmap=4G$64G`, stop and ask the user to do §1b.
- All checks pass → go straight to **§3 build/load**, then **§4 Stage A**.

---

## 1. One-time setup

### 1a. SSD target — DONE
`Kbuild` already has `CONFIG_NVMEVIRT_SSD := y`. SSD geometry/latency knobs (and where the
`READ_RECLAIM_THRESHOLD` config belongs) are in `ssd_config.h`.

### 1b. Reserve physical memory (grub) + reboot — **required, needs you**
This machine has 125 GiB RAM. For **fast read-reclaim testing we want a SMALL device** (a small
region re-read in a loop accumulates per-block reads quickly). Reserve **4 GiB at the 64 GiB
offset**, and isolate two high cores for NVMeVirt:

Edit `/etc/default/grub`, set:
```
GRUB_CMDLINE_LINUX="memmap=4G\\\$64G isolcpus=22,23"
```
Then:
```
! sudo update-grub
! sudo reboot
```
After reboot, confirm: `cat /proc/cmdline` shows `memmap=4G$64G`.
(If `insmod` later panics in `__pci_enable_msix()`/`nvme_hwmon_init()`, add `intremap=off` to the
same line and reboot — IOMMU incompatibility.)

> The `memmap=<size>$<offset>` values MUST match `reload.sh`'s `MEMMAP_SIZE`/`MEMMAP_START`
> (defaults already set to `4G` / `64G`).

### 1c. Install toolchain + fio
```
sudo apt install -y gcc-12 fio
```
The agent may run this directly **if the sudo cache is valid** (preflight showed `cached`);
otherwise ask the user to run it with the `!` prefix.

### 1d. Extend sudo password cache (so iteration doesn't re-prompt) — **needs you**
```
! echo "Defaults timestamp_timeout=120" | sudo tee /etc/sudoers.d/nvmev-timeout && sudo chmod 0440 /etc/sudoers.d/nvmev-timeout && sudo visudo -c
```
Must print `parsed OK`. (`-1` = until logout. Add `Defaults !tty_tickets` if loading from a
different terminal than where you ran `sudo -v`. Undo: `! sudo rm /etc/sudoers.d/nvmev-timeout`.)

---

## 2. Per-session start
The user already ran `sudo -v` once. The default cache TTL is **15 min** (longer only if the §1d
drop-in was installed), so re-prime if any `sudo` command prompts:
```
! sudo -v          # password typed in the user's terminal; never shared in chat
```

---

## 2.5 Slack notifications — message the user at each stage

Slack is **already configured** (webhook + `@here` mention in `~/.claude/.omc-config.json`;
events: session-end, ask-user-question). Two layers:

- **Automatic (hook-based):** if the OMC session was launched with the `--slack` flag
  (`omc --slack ...`, sets `OMC_SLACK=1`), the user is pinged on session-end and whenever the agent
  asks a question. If notifications don't arrive, the flag was likely omitted — tell the user.
- **Explicit per-stage (do this — it's the reliable part):** at the **end of each stage**, the
  agent MUST post a summary via the helper:
  ```bash
  ./notify.sh "Stage A done — baseline seq-read = <BW>, no errors. Proceeding to implement reclaim."
  ./notify.sh "Stage B done — read reclaim implemented (RRT=<N>), builds + loads clean."
  ./notify.sh "Stage C done — reclaim fires (<count> events in dmesg); read BW drops <X>→<Y> vs baseline. Verified."
  ```
  Also notify on a blocker: `./notify.sh "BLOCKED at Stage <X>: <reason>. Need you to <action>."`
  `notify.sh` reads the webhook from the config; HTTP 200 = delivered.

---

## 3. Build → reload (one command)

`reload.sh` does: `make KCFLAGS=-DE820_TYPE_RESERVED_KERN=128` → `rmmod` if loaded → `insmod`
with the memmap params → `dmesg | tail` → list `/dev/nvme*`.

```bash
./reload.sh                          # defaults: MEMMAP_START=64G MEMMAP_SIZE=4G CPUS=22,23
MEMMAP_START=64G MEMMAP_SIZE=4G CPUS=22,23 ./reload.sh   # explicit
```
Manual build only (no load): `make KCFLAGS=-DE820_TYPE_RESERVED_KERN=128`
Unload: `sudo rmmod nvmev`

**Load looks healthy when:** `dmesg` shows `NVMeVirt: Successfully created Virtual NVMe device`
and the dispatcher/worker threads on cpus 22,23; and a new `/dev/nvmeXn1` appears
(likely `/dev/nvme0n1` — **verify** with `lsblk -d -o NAME,SIZE,MODEL`). `/proc/nvmev/` also appears.

---

## 4. STAGE A — Verify baseline NVMeVirt runs well (before reclaim code)

Goal: confirm a clean checkout serves I/O at a steady, sane bandwidth. **Run on the raw device.**
Replace `DEV` with the real node from §3.

```bash
DEV=/dev/nvme0n1

# Minimal-latency knobs (so we measure the emulator, not an artificial cap):
sudo python3 nvmev-evaluation/common/set_perf.py max

# Quick sequential-read sanity (small region, short run):
cd nvmev-evaluation/fio
sudo DEV=$DEV RR_SIZE=2g fio workloads/rr-prep-write.fio          # populate 2 GiB
sudo DEV=$DEV RR_SIZE=2g RR_RUNTIME=30 fio workloads/rr-seq-read.fio
```
**Pass criteria:** the prep-write completes; the seq-read reports a steady bandwidth with no
errors; `dmesg` shows no oops/warnings. This is the **reference** for Stage C — note the steady
bandwidth number. (At this point there is no reclaim code, so bandwidth should be flat.)

→ **Notify (§2.5):** `./notify.sh "Stage A done — baseline seq-read = <BW>, no errors."`

---

## 5. STAGE B — Implement read reclaim (physical NAND block)

Full detail: `read_reclaim_research.md` §6. Summary:

1. **Counter** — add `int read_cnt;` to `struct nand_block` (`ssd.h`).
2. **Increment** — in `conv_read()` (`conv_ftl.c:833`), for each page actually sensed, resolve
   `get_blk(ssd, &ppa)` and `read_cnt++`. Audit the same-flash-page aggregation loop so counting
   matches per-sense semantics.
3. **Reset** — `read_cnt = 0` in `mark_block_free()` (`conv_ftl.c:558`) (runs on erase).
4. **Threshold** — `READ_RECLAIM_THRESHOLD` in `ssd_config.h`, **reads per physical block**.
   *For verification, set it LOW* (see §6) so reclaim fires within seconds.
5. **Reclaim** — when `read_cnt >= RRT`: relocate the block's valid pages (`gc_read_page` /
   `gc_write_page`) and `NAND_ERASE` it. Stock `do_gc()` erases a whole **line** (superblock).
   Recommended first cut = **Option A** (`read_reclaim_research.md` §6.2): count per physical
   block, but reclaim the whole line when any block in it crosses RRT (reuses existing
   free/erase machinery). Option B = true per-block free pool (faithful, more invasive).
6. **Observability (do this for verification)** — add a `printk` (e.g. `NVMEV_INFO`) on each
   reclaim event ("read-reclaim: blk=… read_cnt=… copied=… erased") and a running reclaim
   counter, so `dmesg` directly confirms reclaim is firing. Optionally export via `/proc/nvmev/`.

Rebuild + reload with `./reload.sh` after the change.

→ **Notify (§2.5):** `./notify.sh "Stage B done — read reclaim implemented (RRT=<N>), builds + loads clean."`

---

## 6. STAGE C — Verify the read-reclaim implementation

**Mechanism of the test:** a small region written once (valid pages) is then re-read in a loop.
Each pass adds ~`pgs_per_blk` to every block's `read_cnt`; with a **low RRT** the threshold is
crossed within a few passes, so reclaim fires repeatedly. Each reclaim does valid-page
copies + an erase that **contend with host reads → visible read-bandwidth drops**.

### Pick a low threshold
- `READ_RECLAIM_THRESHOLD` ≈ **1,000–10,000** reads/block for verification (production would be
  10⁴–10⁶). Lower = faster, more frequent reclaim. One sequential pass over a block already adds
  on the order of its page count, so a few thousand triggers reclaim within seconds of looping.

### Run
```bash
DEV=/dev/nvme0n1
sudo python3 nvmev-evaluation/common/set_perf.py max
cd nvmev-evaluation/fio
sudo DEV=$DEV RR_SIZE=2g fio workloads/rr-prep-write.fio              # STEP 1: populate
sudo DEV=$DEV RR_SIZE=2g RR_RUNTIME=180 fio workloads/rr-seq-read.fio # STEP 2: loop-read 180s
```

### Pass criteria (reclaim is working)
1. **`dmesg`** shows recurring read-reclaim events / a climbing reclaim counter during STEP 2.
2. **Bandwidth drops:** the per-second log `rr-read_bw.1.log` (and fio's output) shows periodic
   dips / a lower sustained read bandwidth vs the Stage-A baseline. Inspect with:
   ```bash
   cat rr-read_bw.1.log        # cols: time_ms, bw_KiB/s, ...  -> look for periodic dips
   ```
3. **No corruption / errors:** fio reports 0 read errors; device keeps serving I/O throughout.

### Confirm causation (A/B)
- Set `READ_RECLAIM_THRESHOLD` very high (effectively disabled), rebuild, rerun STEP 2 → bandwidth
  should be **flat** (matches Stage A). Then set it low again → dips reappear. The delta between
  the two runs is the read-reclaim overhead, and is the proof the mechanism works.
- Metrics worth recording per threshold: reclaim count, reclaim-induced page copies, extra erases
  (→ write amplification), mean & p99/p999 read latency, sustained read BW.

→ **Notify (§2.5):** `./notify.sh "Stage C done — reclaim fires (<count> events); read BW <X>→<Y> vs baseline. Verified."`

---

## 7. Files in this setup
- `reload.sh` — build (with KCFLAGS) + reload one-shot. Defaults: `MEMMAP_START=64G MEMMAP_SIZE=4G CPUS=22,23`.
- `notify.sh "msg"` — post a Slack message (used at each stage boundary; see §2.5).
- `nvmev-evaluation/fio/workloads/rr-prep-write.fio` — STEP 1 populate (`RR_SIZE`).
- `nvmev-evaluation/fio/workloads/rr-seq-read.fio` — STEP 2 looping seq-read (`RR_SIZE`, `RR_RUNTIME`), logs per-second BW.
- `read_reclaim_research.md` — background + implementation design (§6 = code hooks).
- `QUICKSTART.md` — durable environment-setup + quick-run guide; keep it updated (see §10).
- Upstream reference: `nvmev-evaluation/FIO_NVMEVIRT_QUICKSTART.md`.

## 8. Notes / gotchas
- **Device size vs `RR_SIZE`:** the emulated logical capacity is < `memmap_size` (4G) due to
  over-provisioning. Keep `RR_SIZE` (default 2g) well below it so there's free space for reclaim's
  valid-page copies. If fio complains the file is too small, lower `RR_SIZE`.
- **Must write before read:** reads of unmapped LPNs are skipped in `conv_read` (no NAND sense, no
  count). The prep-write is mandatory.
- **`set_perf.py max`** removes artificial latency so reclaim's effect on bandwidth is what you see.
- If a run is interrupted: `nvmev-evaluation/common/abort_eval.sh` clears leftover collectors.

---

## 9. Kickoff prompt (paste into a fresh OMC session)

> I'm implementing **block-level read reclaim** in NVMeVirt at `/home/juchanlee/nvmevirt`. Read
> `experiment.md` first (full runbook) and `read_reclaim_research.md` (design; §6 = code hooks).
> I've already installed gcc-12 + fio, reserved memory (`memmap=4G$64G`), rebooted, and run
> `sudo -v`.
>
> Work in order, gating each stage on the previous: (1) preflight per §0; (2) Stage A — build via
> `./reload.sh` and verify baseline NVMeVirt runs well with FIO seq-read, report bandwidth, **do
> not touch reclaim code until this passes**; (3) Stage B — implement read reclaim at **physical
> NAND-block granularity** with a **low threshold** (~1K–10K) and a printk per reclaim; (4) Stage C
> — verify via write-then-loop seq-read showing dmesg reclaim events + read-bandwidth drops vs
> baseline, plus the high/low-threshold A/B.
>
> **At the end of every stage, and on any blocker, run `./notify.sh "<summary>"` to message me on
> Slack** (see experiment.md §2.5). Verify with evidence (dmesg output + bandwidth numbers) before
> claiming a stage done. Ask me before anything needing a reboot.
>
> **Maintain `QUICKSTART.md` (§10):** as you go, record any new environment-setup steps, quirks, or
> fixes in `QUICKSTART.md` (the separate durable setup/quick-run guide) so future experiments are
> turnkey.

---

## 10. Keep the setup/quick-run guide current → `QUICKSTART.md` (maintenance — do as you work)

The durable environment-setup and quick-run guide lives in a **separate file: `QUICKSTART.md`**
(not here). `experiment.md` = staged implementation runbook; `QUICKSTART.md` = turnkey
"how to build, load, run" for this and future experiments. As you work, keep `QUICKSTART.md`
updated:

- **Environment setup:** if you hit a new build/load/run requirement (missing package, kernel-header
  quirk, IOMMU/`intremap` workaround, different device node, `set_perf` knob), add it to
  `QUICKSTART.md` §1–2 with the exact command that fixed it — goal: a fresh machine comes up by
  following that file alone.
- **Quick run:** the copy-paste end-to-end sequence is in `QUICKSTART.md` §3; refine it if the real
  flow differs.
- **Known-good settings:** fill in `QUICKSTART.md` §4 (device node, `RR_SIZE`,
  `READ_RECLAIM_THRESHOLD`, baseline vs reclaim bandwidth) as you converge, so later runs start
  from known-good values.
- Keep edits terse; correct/delete stale content rather than appending contradictions.
