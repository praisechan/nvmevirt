# NVMeVirt — Environment Setup & Quick-Run Guide

> Durable, distilled guide for bringing up NVMeVirt and running read-reclaim experiments on **this
> host**. Keep it current: when you discover a new setup step, quirk, or fix, record it here with
> the exact command. `experiment.md` is the staged implementation runbook; this file is the
> turnkey "how to build, load, and run" reference for repeat experiments.

Host: Linux `6.8.0-111-generic`, 125 GiB RAM, **32 cores** (nproc=32; `CPUS=22,23` are valid).
Repo: `/home/juchanlee/nvmevirt`. (Runbook originally written for `6.5.0-35`; host moved to
`6.8.0-111` — see the build note below.)

> ⚠️ **SHARED MACHINE + device-node safety.** PIN OUR DEVICE BY **IDENTITY, NOT BY NODE NUMBER** —
> the number depends on load order:
> - `/dev/nvme0n1` = **REAL disk** (Crucial `CT2000P3SSD8`, **1.8 TB**) — never write to it.
> - **Other user's** stock `nvmev` @ `memmap=16G$96G` = a **~15 GiB** node — never `rmmod` it, never fio it.
> - **OURS** = the renamed `nvmev_rr`, **`/proc/nvmev_rr` present**,
>   `/sys/module/nvmev_rr/parameters/memmap_start=68719476736` (64G).
>
> **⚠️ DO NOT pin by SIZE any more (updated 2026-06-01).** Our region was enlarged from `4G$64G` to
> **`16G$64G`**, so our device is now a **~15 GiB** node (`ns size 15311 MiB`) — the **same size as the
> other user's**, and the model string `CSL_Virt_MN_01` is shared by both. Size+model can no longer
> tell them apart. **Pin by LOAD-ORDER:** snapshot `/dev/nvme*n1` after `rmmod nvmev_rr` and before
> `insmod`; the **newly-appeared** node is ours, cross-checked by `/proc/nvmev_rr` +
> `memmap_start=64G`. `rr_run.sh` now does exactly this (the old 3827 MiB size-pin was removed).
> **Where this file and `experiment.md` disagree, `experiment.md` wins.**

---

## 1. Environment setup (one-time, needs root)

| Requirement | Why | Command |
|-------------|-----|---------|
| `gcc-12` | kernel was built with gcc-12; gcc-11 rejects `-ftrivial-auto-var-init=zero` | `sudo apt install -y gcc-12` |
| `fio` (+ `sysstat`) | workload generation / stat collection | `sudo apt install -y fio sysstat` |
| Reserved memory + reboot | NVMeVirt maps its device into a reserved physical range | grub step below + reboot |
| SSD build target | read reclaim lives in `conv_ftl.c` | `Kbuild`: `CONFIG_NVMEVIRT_SSD := y` (already set) |
| sudo cache (across ttys) | agent shells use a different tty than your `sudo -v`; default `tty_tickets` makes each sudo re-prompt | `printf 'Defaults timestamp_timeout=120\nDefaults !tty_tickets\n' \| sudo tee /etc/sudoers.d/nvmev-timeout && sudo chmod 0440 /etc/sudoers.d/nvmev-timeout && sudo visudo -c` (then one `sudo -v` covers all ttys for 120 min) |

**Reserve memory (grub):** this is a **shared host** — KEEP the other user's region and APPEND ours.
Edit `/etc/default/grub` so `GRUB_CMDLINE_LINUX` contains **both** (mirror the existing `\$` escaping):
```
GRUB_CMDLINE_LINUX="memmap=16G\$96G memmap=4G\$64G"
```
(= keep theirs 16 GiB@96 GiB; add ours 4 GiB@64 GiB. No `isolcpus` is set on this host — optional.)
Then `sudo update-grub && sudo reboot` (warn the other user first; their device drops until they
re-`insmod` — no grub change needed on their side). After reboot confirm BOTH:
`grep -o 'memmap=[^ ]*' /proc/cmdline` → must show `memmap=16G$96G` **and** `memmap=4G$64G`.
(If `insmod` panics in `__pci_enable_msix()`/`nvme_hwmon_init()`, add `intremap=off` to the same
grub line and reboot — IOMMU incompatibility.)

**Build note (kernel-version dependent):** `main.c:217` uses `E820_TYPE_RESERVED_KERN`.
- On **6.8.0-111** (current host) the headers *define* it (`arch/x86/include/asm/e820/types.h`), so
  the module must be built **clean** — force-defining `-DE820_TYPE_RESERVED_KERN=128` collides with
  the enum (`error: expected identifier before numeric constant`). `reload.sh` now builds with no
  such define.
- On the older **6.5.0-35** headers the symbol was absent and the build *required*
  `KCFLAGS=-DE820_TYPE_RESERVED_KERN=128`. To go back, run `KCFLAGS=-DE820_TYPE_RESERVED_KERN=128 ./reload.sh`.

**Module signing — NOT required on this host (updated 2026-05-31).** Secure Boot is **DISABLED**
(`mokutil --sb-state` = disabled, Setup Mode), kernel lockdown = none, `sig_enforce = N`. Plain
`insmod` of the unsigned module **works** — no MOK signing needed. `reload.sh`'s signing block
self-skips when no MOK key is present (harmless). *(The previous instructions here assumed Secure
Boot was enabled with MOK signing — that was a **different server** and does not apply to this host.)*

---

## 2. Build & load

```bash
cd /home/juchanlee/nvmevirt
sudo -v                 # prime sudo cache (password in your terminal)
./reload.sh             # make (KCFLAGS) -> rmmod -> insmod (memmap_start=64G size=4G cpus=22,23) -> dmesg
```
Healthy load: `dmesg` shows `NVMeVirt: Virtual NVMe device created`; a new `/dev/nvmeXn1` appears
(verify it's the **3.7 GiB** node: `lsblk -d -o NAME,SIZE,MODEL`); `/proc/nvmev_rr/` exists.
Unload **ours only**: `sudo rmmod nvmev_rr` (NEVER `rmmod nvmev` — other user's). Build only (6.8): `make`.
(`reload.sh` detects an already-loaded module via `lsmod | grep -qw nvmev_rr` and rmmods it first;
if `insmod` ever reports `File exists`, the old module is still resident — `sudo rmmod nvmev_rr`.)

---

## 3. Quick run (read-reclaim experiment)

> Reads only accumulate on blocks that hold **valid data**, so a write pass must precede the read
> loop. Re-reading a small region in a loop drives per-block read counts up fast.

**Easiest (one command, auto-pins our device by size, clears dmesg, reports reclaims + BW):**
```bash
cd /home/juchanlee/nvmevirt
sudo -v                                  # prime sudo (once; lasts 120 min via the drop-in)
./rr_run.sh reclaim 180                   # reload (current RRT) + prep-write + 180s seq-read + report
# A/B control: set READ_RECLAIM_THRESHOLD to 1000000000 in ssd_config.h, then:
./rr_run.sh disabled 180                  # 0 reclaims, flat BW = baseline; revert RRT to 8 after
# results in rr_results/{fio_<label>.log, bw_<label>.log}
```
**Manual equivalent** (note `set_perf_rr.py`, NOT `set_perf.py` — the latter writes `/proc/nvmev`,
ours is `/proc/nvmev_rr`):
```bash
DEV=/dev/nvme1n1        # ⚠️ VERIFY = our 3.7 GiB node (lsblk). NOT nvme0n1 (1.8T real), NOT the 15G other-user node
cd /home/juchanlee/nvmevirt && ./reload.sh
sudo python3 nvmev-evaluation/common/set_perf_rr.py max       # remove artificial latency (targets /proc/nvmev_rr)
cd nvmev-evaluation/fio
sudo DEV=$DEV RR_SIZE=2g fio workloads/rr-prep-write.fio              # STEP 1: populate valid pages
sudo DEV=$DEV RR_SIZE=2g RR_RUNTIME=180 fio workloads/rr-seq-read.fio # STEP 2: looping seq-read
cat rr-read_bw.1.log              # per-second bandwidth -> reclaim-induced dips
sudo dmesg | grep 'read-reclaim:' # reclaim events (printk added in Stage B)
```
Notify when done: `/home/juchanlee/nvmevirt/notify.sh "run done — <summary>"`.

---

## 4. Known-good settings (fill in / update as you converge)

> **Current device = 16 GiB (updated 2026-06-01).** Region enlarged to `memmap=16G$64G`; see
> `reports/0601_report_rrt-sweep-16g.md` for the geometry + RRT sweep. The 4 GiB values below are the
> earlier (2026-05-31) state, kept for contrast.

| Setting | Value | Notes |
|---------|-------|-------|
| `memmap` (ours) | **`16G$64G`** (was `4G$64G`) | enlarged for realistic block geometry. Theirs = `16G$96G` (leave it) |
| Module name (ours) | `nvmev_rr` | renamed from `nvmev` to coexist with other user (experiment.md §1e) |
| `CPUS` / `isolcpus` | `22,23` (nproc=32, so valid) | NVMeVirt dispatcher + worker; no isolcpus set on this host |
| Device node (ours) | **`/dev/nvme1n1`** (~15 GiB at 16G); **VERIFY by LOAD-ORDER + `/proc/nvmev_rr` + `memmap_start=64G`, NOT by size** | size now collides with other user's ~15 GiB node |
| `RR_SIZE` | `512m` (16G sweep); was `2g` on 4G device | smaller region = faster passes so higher RRT fires within 180 s |
| `READ_RECLAIM_THRESHOLD` | **8** (default, restored); `1000000000` = disabled (A/B control) | reads/block, **geometry-dependent**: 16G block = 4 flash pages ⇒ +4/pass (4G block = 1 page ⇒ +1/pass). In `ssd_config.h` (`#ifndef`-guarded). |
| Geometry (16G, per FTL instance) | blk **128 KiB** (pgs_per_blk **32**, flashpgs_per_blk **4**); line **512 KiB** (pgs_per_line **128**, blks_per_line **4**); 8192 lines | vs 4G: 32 KiB blk (8 / 1) |
| Baseline seq-read BW (16G) | **flat ~964 MiB/s** (RRT disabled; min 940, max 968) | reference for the sweep |
| Reclaim seq-read BW (16G) | RRT 16/64/256/1024 → avg **838.6 / 930.2 / 956.8 / 963.8 MiB/s**; dip floor ~650–740 MiB/s | reclaims/instance 19712 / 5120 / 1280 / 256; 0 fio errors. RRT=64 = good default (−3.5%) |
| Reclaim seq-read BW (4G, 2026-05-31) | **avg 954 MiB/s, dips to ~652** (RRT=8); baseline ~1003–1006 | ~36% dips; 45057+ reclaims/instance/180 s |

---

## 5. Cleanup / troubleshooting
- Stop leftover fio/stat collectors: `nvmev-evaluation/common/abort_eval.sh`
- Reset the device state (ours only): `sudo rmmod nvmev_rr && ./reload.sh`  (NEVER `rmmod nvmev` — other user's)
- Upstream reference: `nvmev-evaluation/FIO_NVMEVIRT_QUICKSTART.md`
