# NVMeVirt — Environment Setup & Quick-Run Guide

> Durable, distilled guide for bringing up NVMeVirt and running read-reclaim experiments on **this
> host**. Keep it current: when you discover a new setup step, quirk, or fix, record it here with
> the exact command. `experiment.md` is the staged implementation runbook; this file is the
> turnkey "how to build, load, and run" reference for repeat experiments.

Host: Linux `6.8.0-111-generic`, 125 GiB RAM, 24 cores. Repo: `/home/juchanlee/nvmevirt`.
(Runbook was originally written for `6.5.0-35`; the host has since moved to `6.8.0-111` — see the
build note below, which changed as a result.)

> ⚠️ **Device-node safety (this host):** `/dev/nvme0n1` is the **real 1 TB Samsung SSD 960 PRO**.
> NVMeVirt's emulated device appears as a *different* node after `insmod` (e.g. `/dev/nvme1n1`,
> small ~3.6 GiB). **Always** identify the virtual node via `lsblk -d -o NAME,SIZE,MODEL` /
> `dmesg` and point fio at THAT — never at `nvme0n1`, or you will overwrite the real disk.

---

## 1. Environment setup (one-time, needs root)

| Requirement | Why | Command |
|-------------|-----|---------|
| `gcc-12` | kernel was built with gcc-12; gcc-11 rejects `-ftrivial-auto-var-init=zero` | `sudo apt install -y gcc-12` |
| `fio` (+ `sysstat`) | workload generation / stat collection | `sudo apt install -y fio sysstat` |
| Reserved memory + reboot | NVMeVirt maps its device into a reserved physical range | grub step below + reboot |
| SSD build target | read reclaim lives in `conv_ftl.c` | `Kbuild`: `CONFIG_NVMEVIRT_SSD := y` (already set) |
| (optional) sudo cache | avoid re-prompting during iteration | `echo "Defaults timestamp_timeout=120" \| sudo tee /etc/sudoers.d/nvmev-timeout && sudo chmod 0440 /etc/sudoers.d/nvmev-timeout && sudo visudo -c` |

**Reserve memory (grub):** edit `/etc/default/grub`, set inside the quotes of `GRUB_CMDLINE_LINUX`:
```
memmap=4G\$64G isolcpus=22,23
```
(= reserve 4 GiB at the 64 GiB offset; isolate cores 22,23 for NVMeVirt. Safe on 125 GiB RAM.)
Then `sudo update-grub && sudo reboot`. After reboot confirm: `grep -o 'memmap=[^ ]*' /proc/cmdline`
→ must show `memmap=4G$64G`.
(If `insmod` panics in `__pci_enable_msix()`/`nvme_hwmon_init()`, add `intremap=off` to the same
grub line and reboot — IOMMU incompatibility.)

**Build note (kernel-version dependent):** `main.c:217` uses `E820_TYPE_RESERVED_KERN`.
- On **6.8.0-111** (current host) the headers *define* it (`arch/x86/include/asm/e820/types.h`), so
  the module must be built **clean** — force-defining `-DE820_TYPE_RESERVED_KERN=128` collides with
  the enum (`error: expected identifier before numeric constant`). `reload.sh` now builds with no
  such define.
- On the older **6.5.0-35** headers the symbol was absent and the build *required*
  `KCFLAGS=-DE820_TYPE_RESERVED_KERN=128`. To go back, run `KCFLAGS=-DE820_TYPE_RESERVED_KERN=128 ./reload.sh`.

**Module signing (Secure Boot — required on this host):** Secure Boot is **enabled** and the kernel
is locked down (`/sys/module/module/parameters/sig_enforce = Y`), so `insmod` rejects unsigned
modules with `Key was rejected by service`. Fix = sign the module with an enrolled **MOK**:
1. Key already generated at `/var/lib/shim-signed/mok/MOK.{key,der}` (RSA-2048, CN=NVMeVirt Local
   Module Signing). Regenerate with `openssl req -new -x509 -newkey rsa:2048 -keyout MOK.key -out
   MOK.der -outform DER -days 36500 -nodes -subj "/CN=NVMeVirt Local Module Signing/"`.
2. **One-time enroll (needs reboot):** `sudo mokutil --import /var/lib/shim-signed/mok/MOK.der`
   (set a one-time password) → `sudo reboot` → at the blue **MOK Manager** screen choose
   *Enroll MOK → Continue → Yes*, re-enter the password. Verify after boot:
   `mokutil --test-key /var/lib/shim-signed/mok/MOK.der` → "is already enrolled".
3. `reload.sh` then auto-signs every rebuild via `scripts/sign-file` before `insmod` (override key
   paths with `MOK_KEY`/`MOK_DER` env vars). Manual sign:
   `sudo /usr/src/linux-headers-$(uname -r)/scripts/sign-file sha256 MOK.key MOK.der nvmev.ko`.
Alternative (not preferred): disable Secure Boot in firmware — also a reboot, weakens system security.

---

## 2. Build & load

```bash
cd /home/juchanlee/nvmevirt
sudo -v                 # prime sudo cache (password in your terminal)
./reload.sh             # make (KCFLAGS) -> rmmod -> insmod (memmap_start=64G size=4G cpus=22,23) -> dmesg
```
Healthy load: `dmesg` shows `NVMeVirt: Successfully created Virtual NVMe device`; a new
`/dev/nvmeXn1` appears (verify: `lsblk -d -o NAME,SIZE,MODEL`); `/proc/nvmev/` exists.
Unload: `sudo rmmod nvmev`. Build only (6.8): `make`.

---

## 3. Quick run (read-reclaim experiment)

> Reads only accumulate on blocks that hold **valid data**, so a write pass must precede the read
> loop. Re-reading a small region in a loop drives per-block read counts up fast.

```bash
DEV=/dev/nvme1n1        # ⚠️ the NVMeVirt virtual node — VERIFY with lsblk; NOT nvme0n1 (real disk)
cd /home/juchanlee/nvmevirt
./reload.sh
sudo python3 nvmev-evaluation/common/set_perf.py max          # remove artificial latency
cd nvmev-evaluation/fio
sudo DEV=$DEV RR_SIZE=2g fio workloads/rr-prep-write.fio              # STEP 1: populate valid pages
sudo DEV=$DEV RR_SIZE=2g RR_RUNTIME=180 fio workloads/rr-seq-read.fio # STEP 2: looping seq-read
# inspect:
cat rr-read_bw.1.log     # per-second bandwidth -> look for reclaim-induced dips
dmesg | grep -i reclaim  # reclaim events (needs the printk added in Stage B)
```
Notify when done: `/home/juchanlee/nvmevirt/notify.sh "run done — <summary>"`.

---

## 4. Known-good settings (fill in / update as you converge)

| Setting | Value | Notes |
|---------|-------|-------|
| `memmap` | `4G$64G` | small device = fast reclaim |
| `CPUS` / `isolcpus` | `22,23` | NVMeVirt dispatcher + worker |
| Device node | `/dev/nvme1n1` (VERIFY; **nvme0n1 = real disk**) | NVMeVirt virtual node for fio |
| `RR_SIZE` | `2g` | keep < logical capacity (OP-reduced) |
| `READ_RECLAIM_THRESHOLD` | _TBD_ | start ~1K–10K for fast verification |
| Baseline seq-read BW | _TBD_ | reference for reclaim comparison |
| Reclaim seq-read BW | _TBD_ | should dip below baseline |

---

## 5. Cleanup / troubleshooting
- Stop leftover fio/stat collectors: `nvmev-evaluation/common/abort_eval.sh`
- Reset the device state: `sudo rmmod nvmev && ./reload.sh`
- Upstream reference: `nvmev-evaluation/FIO_NVMEVIRT_QUICKSTART.md`
