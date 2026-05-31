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

**Host:** Linux `6.8.0-111-generic`, ~125 GiB RAM. Working dir `/home/juchanlee/nvmevirt`.
(Core count: verify with `nproc` — the original runbook assumed 24. Pick CPU IDs that exist, see §3.)

> ⚠️ **SHARED MACHINE — another user runs their own NVMeVirt here.** A second user's *stock* `nvmev`
> module is (or will be) loaded and owns the **only pre-existing reserved region** `memmap=16G$96G`
> → their device is **`/dev/nvme1n1`** (model `CSL_Virt_MN_01`). **Never `rmmod` their module, never
> point fio at `/dev/nvme1n1`, and never run the *stock* `reload.sh`** — stock `reload.sh` does
> `rmmod nvmev` (would destroy their device) and its old default region `64G/4G` was not reserved
> until §1b. The coexistence plan (**Option A**, decided 2026-05-31) makes our module separate and
> independently loadable, so they can load theirs whenever they want:
>   - **Rename our module** `nvmev` → `nvmev_rr` (own name + own `/proc/nvmev_rr`) — **§1e** (agent).
>   - **Add a 2nd reserved region** `memmap=4G$64G` (small device = fast reclaim) — **§1b** (user; reboot).
>   - Result: theirs = `nvmev` @ 96G/16G → `/dev/nvme1n1`; **ours = `nvmev_rr` @ 64G/4G →
>     `/dev/nvme2n1`** (verify node with `lsblk`). Either loads anytime; we never touch theirs.

> ✅ **Secure Boot is DISABLED on this host** (`mokutil --sb-state`=disabled, `sig_enforce=N`,
> lockdown=none). Plain `insmod` of the unsigned module **works — no MOK signing needed.** *Ignore*
> the "Secure Boot enabled / signing required" note in `QUICKSTART.md §1`: it was copied from a
> previous server and is **stale**. (`reload.sh`'s signing block self-skips when no MOK key exists —
> harmless.) **Trust this file over `QUICKSTART.md` where they disagree.**

> ℹ️ **Disk identity (always verify: `lsblk -d -o NAME,SIZE,MODEL`):** `/dev/nvme0n1` = **REAL disk**
> (Crucial `CT2000P3SSD8`, 1.8 TB) — never write to it. `/dev/nvme1n1` = **other user's** nvmev
> device. Our device will be the next free node (expected `/dev/nvme2n1`).

**Already done in a previous session (do NOT redo):**
- `Kbuild` set to `CONFIG_NVMEVIRT_SSD := y` (builds `conv_ftl.c`, base SAMSUNG_970PRO).
- `reload.sh`, FIO workloads (`rr-prep-write.fio`, `rr-seq-read.fio`), `read_reclaim_research.md` exist.
- **Toolchain present:** gcc-12 (12.3.0) and fio (3.28) are installed.

**NOT yet done — do these first, in order:**
1. **Module rename → `nvmev_rr`** (§1e) — agent; edits our copy of `Kbuild`, `main.c`, `reload.sh`.
2. **2nd memmap region `4G$64G` + coordinated reboot** (§1b) — user only.
3. **Read-reclaim code is NOT implemented** (Stage B).

### Preflight checks — run these first; fix any that fail before proceeding
```bash
cd /home/juchanlee/nvmevirt
echo "--- gcc-12 ---"; gcc-12 --version 2>/dev/null | head -1 || echo "MISSING -> sudo apt install -y gcc-12"
echo "--- fio ---";    fio --version 2>/dev/null || echo "MISSING -> sudo apt install -y fio"
echo "--- memmap regions (need BOTH: 16G\$96G theirs + 4G\$64G ours) ---"; grep -o 'memmap=[^ ]*' /proc/cmdline || echo "NONE -> grub §1b + REBOOT (user only)"
echo "--- our region present? ---"; grep -qF 'memmap=4G$64G' /proc/cmdline && echo "yes" || echo "MISSING -> §1b grub + reboot"
echo "--- module renamed? ---"; grep -q 'nvmev_rr' Kbuild && echo "renamed (nvmev_rr)" || echo "NOT renamed -> do §1e"
echo "--- other user's module (LEAVE IT) ---"; lsmod | grep -qw nvmev && echo "nvmev loaded (theirs)" || echo "nvmev not loaded"
echo "--- our module loaded? ---"; lsmod | grep -qw nvmev_rr && echo "nvmev_rr loaded (ours)" || echo "nvmev_rr not loaded"
echo "--- sudo cache ---"; sudo -n true 2>/dev/null && echo "cached" || echo "expired -> ask user: ! sudo -v"
```

**Who can fix what:**
- `gcc-12` / `fio` missing → `sudo apt install -y gcc-12 fio` (agent may run it if sudo cached).
- **Module rename (§1e) → agent** (edits *our* copy only: `Kbuild`, `main.c`, `reload.sh`). Do before any build.
- **2nd memmap region `4G$64G` (§1b) → user only** (grub edit + **coordinated reboot**; agent cannot
  reboot). The one hard gate. If `/proc/cmdline` lacks `memmap=4G$64G`, stop and ask the user (and
  remind them to warn the other user — the reboot drops that user's device until they re-`insmod`).
- Rename done **and** both memmap regions present → go to **§3 build/load**, then **§4 Stage A**.

---

## 1. One-time setup

### 1a. SSD target — DONE
`Kbuild` already has `CONFIG_NVMEVIRT_SSD := y`. SSD geometry/latency knobs (and where the
`READ_RECLAIM_THRESHOLD` config belongs) are in `ssd_config.h`.

### 1b. Add OUR reserved region (grub) + one coordinated reboot — **required, needs you**
This is a **shared machine**. The other user's region `memmap=16G$96G` is already in the cmdline and
**must be kept**. We **append a second, small region** for our device: `memmap=4G$64G` (4 GiB at the
64 GiB offset; small device = fast read-reclaim). 64–68 GiB does not overlap their 96–112 GiB.

Edit `/etc/default/grub`, set `GRUB_CMDLINE_LINUX` to contain **both** regions (keep theirs, add ours,
and preserve any other tokens already on the line):
```
GRUB_CMDLINE_LINUX="memmap=16G\$96G memmap=4G\$64G"
```
Then — **after warning the other user, since the reboot drops their device until they re-`insmod`**:
```
! sudo update-grub
! sudo reboot
```
After reboot, confirm BOTH appear: `grep -o 'memmap=[^ ]*' /proc/cmdline`
→ must show `memmap=16G$96G` **and** `memmap=4G$64G`.
(Optional, for cleaner bandwidth numbers: also add `isolcpus=<two high core IDs that exist on this
host>` and use the same IDs for `CPUS` in §3 — this host currently has **no** isolcpus.)
(If `insmod` later panics in `__pci_enable_msix()`/`nvme_hwmon_init()`, add `intremap=off` to the same
line and reboot — IOMMU incompatibility.)

> Our `memmap=4G$64G` MUST match `reload.sh`'s `MEMMAP_START=64G` / `MEMMAP_SIZE=4G` (already the
> defaults). The other user's `16G$96G` is theirs — we never load into it.

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

### 1e. Rename our module so it coexists with the other user's `nvmev` — **agent, one-time**
NVMeVirt is a **singleton**: module name `nvmev`, one global `nvmev_vdev`, fixed `/proc/nvmev`. Two
modules of the same name **cannot both be loaded**, so our experiment module is renamed to
**`nvmev_rr`**. This touches **only our copy**; the other user keeps stock `nvmev`. Three edits:

1. **`Kbuild`** — rename the module + its composite make-vars (`nvmev-…` → `nvmev_rr-…`):
   - `obj-m   := nvmev.o`   →   `obj-m   := nvmev_rr.o`
   - `nvmev-objs := …`   →   `nvmev_rr-objs := …`
   - every `nvmev-$(CONFIG_NVMEVIRT_*) += …`   →   `nvmev_rr-$(CONFIG_NVMEVIRT_*) += …`
2. **`main.c`** — the two fixed `/proc` names (else our init collides with their `/proc/nvmev`):
   - `proc_mkdir("nvmev", NULL)`        →   `proc_mkdir("nvmev_rr", NULL)`
   - `remove_proc_entry("nvmev", NULL)` →   `remove_proc_entry("nvmev_rr", NULL)`
3. **`reload.sh`** — point it at our module so its `rmmod` can NEVER hit theirs:
   - `MODNAME=nvmev`   →   `MODNAME=nvmev_rr`   (build output becomes `nvmev_rr.ko`; the insmod/sign
     lines already use `${MODNAME}`).

(Optional: `Makefile`'s `dis` target objdumps `nvmev.ko` — bump to `nvmev_rr.ko` only if you use it.)
After the rename: `make` produces `nvmev_rr.ko`; load via `./reload.sh` (§3). Our device appears as a
**new** node (expected `/dev/nvme2n1`) plus `/proc/nvmev_rr/`; theirs (`/dev/nvme1n1`, `/proc/nvmev`)
is untouched and stays loadable at any time.

---

## 2. Per-session start
The user already ran `sudo -v` once. The default cache TTL is **15 min** (longer only if the §1d
drop-in was installed), so re-prime if any `sudo` command prompts:
```
! sudo -v          # password typed in the user's terminal; never shared in chat
```

---

## 2.5 Slack notifications — message the user at each stage

Slack **is configured and verified working** (incoming webhook in `~/.claude/.omc-config.json`;
verified 2026-05-31 → `./notify.sh` returns `HTTP 200`. No `mention` is set; no automatic hook
events — the explicit helper below is the path to use).

- **End of each stage** — post a summary:
  ```bash
  ./notify.sh "Stage A done — baseline seq-read = <BW>, no errors. Proceeding to implement reclaim."
  ./notify.sh "Stage B done — read reclaim implemented (RRT=<N>), builds + loads clean."
  ./notify.sh "Stage C done — reclaim fires (<count> events in dmesg); read BW drops <X>→<Y> vs baseline. Verified."
  ```
- **Blocker:** `./notify.sh "BLOCKED at Stage <X>: <reason>. Need you to <action>."`
- **Decision / permission needed (do this every time):** whenever you need the user's choice,
  permission, or a decision — e.g. the §1b reboot, picking `CPUS`, or any destructive / hard-to-reverse
  / ambiguous step — run `./notify.sh "NEED YOUR CALL: <question> — options: <A/B>"`, then **pause and
  wait** for the reply. Never silently guess on such steps.

`notify.sh` reads the webhook from the config; `HTTP 200` = delivered.

---

## 3. Build → reload (one command)

**Prereqs:** §1e rename done, and `memmap=4G$64G` present in `/proc/cmdline` (§1b). On kernel
6.8.0-111 build **clean** — **no `KCFLAGS`** (the `-DE820_TYPE_RESERVED_KERN=128` define is for the
old 6.5 kernel and *breaks* the 6.8 build; `reload.sh` already defaults `KCFLAGS` empty).

`reload.sh` does: `make` → (sign if MOK present — skipped here, SB off) → `rmmod $MODNAME` if loaded
→ `insmod` with the memmap params → `dmesg | tail` → list `/dev/nvme*`. With `MODNAME=nvmev_rr`, its
`rmmod` only ever touches OUR module.

```bash
# CPUS: pick two core IDs that EXIST on this host (check: nproc). isolcpus is NOT set here.
MEMMAP_START=64G MEMMAP_SIZE=4G CPUS=<c1>,<c2> ./reload.sh
./reload.sh                          # uses defaults MEMMAP_START=64G MEMMAP_SIZE=4G CPUS=22,23
```
Manual build only (no load): `make`
Unload **ours only**: `sudo rmmod nvmev_rr`   — **NEVER `rmmod nvmev`** (that's the other user's).

**Load looks healthy when:** `dmesg` shows `NVMeVirt: Successfully created Virtual NVMe device`;
a **new** `/dev/nvmeXn1` appears — expected **`/dev/nvme2n1`** (small, ≈3.6 GiB) — **verify** with
`lsblk -d -o NAME,SIZE,MODEL` (NOT `nvme0n1`=real Crucial disk, NOT `nvme1n1`=other user's nvmev);
and `/proc/nvmev_rr/` appears.

---

## 4. STAGE A — Verify baseline NVMeVirt runs well (before reclaim code)

Goal: confirm a clean checkout serves I/O at a steady, sane bandwidth. **Run on the raw device.**
Replace `DEV` with the real node from §3.

```bash
DEV=/dev/nvme2n1   # ⚠️ VERIFY via lsblk = OUR nvmev_rr node. NOT nvme0n1 (real disk), NOT nvme1n1 (other user)

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
   counter, so `dmesg` directly confirms reclaim is firing. Optionally export via `/proc/nvmev_rr/`.

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
DEV=/dev/nvme2n1   # ⚠️ VERIFY via lsblk = OUR nvmev_rr node. NOT nvme0n1 (real disk), NOT nvme1n1 (other user)
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
> `experiment.md` first (full runbook — **trust it over `QUICKSTART.md` where they disagree**) and
> `read_reclaim_research.md` (design; §6 = code hooks).
>
> ⚠️ **This is a SHARED machine.** Another user runs their own stock `nvmev` module at
> `memmap=16G$96G` → their device is `/dev/nvme1n1` (`CSL_Virt_MN_01`). **Do not `rmmod` it, do not
> point fio at it, do not run stock `reload.sh`.** Real disk is `/dev/nvme0n1` (Crucial, 1.8 TB) —
> never touch it. We coexist via **Option A**: our module is renamed `nvmev_rr` with its own region
> `memmap=4G$64G` → our device `/dev/nvme2n1` (verify with `lsblk`). gcc-12 + fio are installed;
> Secure Boot is OFF (no signing). The rename (§1e) and the 2nd memmap region + reboot (§1b) are NOT
> done yet.
>
> Work in order, gating each stage on the previous: (1) **preflight per §0**; (2) **§1e rename** to
> `nvmev_rr` (agent); (3) **§1b** — ask me to add `memmap=4G$64G` + reboot (I'll warn the other
> user); (4) Stage A — build via `./reload.sh`, **verify the device node is ours (`nvme2n1`, NOT
> `nvme0/1`)**, verify baseline seq-read bandwidth, **do not touch reclaim code until this passes**;
> (5) Stage B — implement read reclaim at **physical NAND-block granularity** with a **low
> threshold** (~1K–10K) and a printk per reclaim; (6) Stage C — verify via write-then-loop seq-read
> showing dmesg reclaim events + read-bandwidth drops vs baseline, plus the high/low-threshold A/B.
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
