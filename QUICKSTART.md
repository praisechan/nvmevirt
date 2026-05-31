# NVMeVirt — Environment Setup & Quick-Run Guide

> Durable, distilled guide for bringing up NVMeVirt and running read-reclaim experiments on **this
> host**. Keep it current: when you discover a new setup step, quirk, or fix, record it here with
> the exact command. `experiment.md` is the staged implementation runbook; this file is the
> turnkey "how to build, load, and run" reference for repeat experiments.

Host: Linux `6.8.0-111-generic`, 125 GiB RAM, **32 cores** (nproc=32; `CPUS=22,23` are valid).
Repo: `/home/juchanlee/nvmevirt`. (Runbook originally written for `6.5.0-35`; host moved to
`6.8.0-111` — see the build note below.)

> ⚠️ **SHARED MACHINE + device-node safety (this host, verified 2026-05-31).** PIN OUR DEVICE BY
> **IDENTITY, NOT BY NODE NUMBER** — the number depends on load order:
> - `/dev/nvme0n1` = **REAL disk** (Crucial `CT2000P3SSD8`, **1.8 TB**) — never write to it.
> - **Other user's** stock `nvmev` @ `memmap=16G$96G` = a **15 GiB** node — never `rmmod` it, never fio it.
> - **OURS** = the renamed `nvmev_rr` @ `memmap=4G$64G` = the **3827 MiB (3.7 GiB)** node, with
>   **`/proc/nvmev_rr` present** and `/sys/module/nvmev_rr/parameters/memmap_start=64G`.
>
> The kernel names ours `nvme2n1` only if the other user loaded first; if they are NOT loaded, ours
> becomes **`nvme1n1`** (as observed 2026-05-31). The model string `CSL_Virt_MN_01` is shared by BOTH
> modules — useless for telling them apart; **use SIZE (3.7 GiB) + `/proc/nvmev_rr`.** `rr_run.sh`
> auto-pins by the 3827 MiB size. **Where this file and `experiment.md` disagree, `experiment.md` wins.**

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

| Setting | Value | Notes |
|---------|-------|-------|
| `memmap` (ours) | `4G$64G` | 2nd region we add; small device = fast reclaim. Theirs = `16G$96G` (leave it) |
| Module name (ours) | `nvmev_rr` | renamed from `nvmev` to coexist with other user (experiment.md §1e) |
| `CPUS` / `isolcpus` | `22,23` (nproc=32, so valid) | NVMeVirt dispatcher + worker; no isolcpus set on this host |
| Device node (ours) | the **3.7 GiB** node (`nvme1n1` when other user not loaded; VERIFY by size + `/proc/nvmev_rr`) | our nvmev_rr node for fio |
| `RR_SIZE` | `2g` | keep < logical capacity (OP-reduced). Device logical ≈ 3827 MiB |
| `READ_RECLAIM_THRESHOLD` | **8** (verification); `1000000000` = disabled (A/B control) | reads/block. NOTE geometry: a block here is **1 flash page**, so a seq pass adds only **+1** per block ⇒ the doc's 1K–10K would need ~30 min to fire; 8 fires within ~8 s. Production HW = 1e4–1e6. In `ssd_config.h` (`#ifndef`-guarded). |
| Baseline seq-read BW | **~1003–1006 MiB/s** (reclaim disabled; flat, min 983) | reference (Stage A and RRT-disabled control agree) |
| Reclaim seq-read BW | **avg 954 MiB/s, dips to ~652 MiB/s** (RRT=8) | ~36% periodic dips during reclaim bursts; 45057+ reclaim events/instance in 180 s; 0 fio errors |

---

## 5. Cleanup / troubleshooting
- Stop leftover fio/stat collectors: `nvmev-evaluation/common/abort_eval.sh`
- Reset the device state (ours only): `sudo rmmod nvmev_rr && ./reload.sh`  (NEVER `rmmod nvmev` — other user's)
- Upstream reference: `nvmev-evaluation/FIO_NVMEVIRT_QUICKSTART.md`
