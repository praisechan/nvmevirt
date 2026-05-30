# Session Summary — Read-Reclaim Experiment Setup (2026-05-30)

Working dir: `/home/juchanlee/nvmevirt` · Host: ASUS PRIME X399-A desktop, kernel `6.8.0-111-generic`,
125 GiB RAM, 24 cores.

## Goal
Follow `experiment.md`: (A) verify baseline NVMeVirt with FIO seq-read → (B) implement physical
NAND-block read reclaim → (C) verify reclaim fires with read-bandwidth drops. Stages are gated in
order.

## Status: BLOCKED at Stage A (cannot load the kernel module remotely)

Reached the `insmod` step and hit a hard environmental gate: **Secure Boot**. No code from Stage B/C
was written (the runbook forbids touching reclaim code until the Stage A baseline passes at runtime,
and the baseline cannot run until the module loads).

```
#1 Stage A — verify baseline NVMeVirt (FIO seq-read)   [IN PROGRESS — blocked on module load]
#2 Stage B — implement physical-block read reclaim     [pending]
#3 Stage C — verify read-reclaim fires + BW drops      [pending]
```

## What was done (all no-reboot prep complete)

### Preflight (§0)
- ✅ `memmap=4G$64G isolcpus=22,23` present in `/proc/cmdline` — the memory-reservation hard gate
  passes; **no reboot needed for memmap**.
- ✅ gcc-12 (12.3.0) and fio (3.28) installed.
- ✅ sudo drop-in `/etc/sudoers.d/nvmev-timeout` present.

### Fix 1 — sudo cache never cached over SSH (tty_tickets)
`sudo -v` in the user's terminal didn't apply to the tool shell because `tty_tickets` (default on)
scopes the credential per-tty. Fixed by rewriting the drop-in to add `Defaults !tty_tickets`
alongside `Defaults timestamp_timeout=120`. After re-priming, `sudo -n true` succeeds in the tool
shell.

### Fix 2 — baseline build failed on kernel 6.8 (`E820_TYPE_RESERVED_KERN`)
The runbook (written for 6.5.0-35) forces `KCFLAGS=-DE820_TYPE_RESERVED_KERN=128` because that
kernel lacked the symbol. Kernel **6.8.0-111 now defines it** in
`arch/x86/include/asm/e820/types.h`, so force-defining it collides with the enum:
`error: expected identifier before numeric constant`. Fix: build **clean** (no such define).
- `reload.sh` updated: `KCFLAGS` now defaults to empty.
- Result: `nvmev.ko` builds cleanly on 6.8.0-111.

### Fix 3 (infra) — Secure Boot module signing (the actual blocker)
`insmod` fails with **`Key was rejected by service`**. Diagnosis:
```
SecureBoot enabled
Kernel is locked down from EFI Secure Boot mode (lockdown=integrity)
/sys/module/module/parameters/sig_enforce = Y
```
The kernel refuses any unsigned out-of-tree module. This is a property of the **host**, not of
NVMeVirt — the upstream README's `insmod` works only on machines with Secure Boot off. Running
`insmod` as a different user does not help; the rejection is from the kernel.

There is **no OS-side bypass**: `sig_enforce` can't be cleared under lockdown, the firmware/Secure
Boot variables can't be written from the OS, and an unsigned kernel/cmdline can't be booted under
Secure Boot. The only fixes require one physical-console action.

Prepared everything that does **not** need a reboot:
- Generated a Machine Owner Key: `/var/lib/shim-signed/mok/MOK.{key,der}` (RSA-2048,
  CN=`NVMeVirt Local Module Signing`).
- `reload.sh` now **auto-signs** `nvmev.ko` with `scripts/sign-file` after every build, before
  `insmod` (override via `MOK_KEY`/`MOK_DER` env).
- Test-signed the current `nvmev.ko` — signs OK.

### Code study for Stage B (ready to implement)
Confirmed the implementation plan (research §6.2 **Option A**): per-physical-block `read_cnt` in
`struct nand_block` (`ssd.h`); increment per flash-page sense at the two `ssd_advance_nand(&srd)`
sites in `conv_read()`; reset in `mark_block_free()`; `READ_RECLAIM_THRESHOLD` (low, ~1K–10K) in
`ssd_config.h`; reclaim the whole **line** when any block crosses the threshold, **deferred to the
end of the per-part read loop** (avoids stale-PPA corruption mid-aggregation), reusing the `do_gc`
relocate+erase machinery; `pqueue_remove()` is available to pull a full/victim line from its queue;
add an `NVMEV_INFO` printk + running counter per reclaim event.

## ⚠️ Critical safety note carried forward
`/dev/nvme0n1` is the **real 1 TB Samsung SSD 960 PRO** (the system disk). The NVMeVirt virtual
device will appear as a *different*, small (~3.6 GiB) node (e.g. `/dev/nvme1n1`) after a successful
load. **FIO must target the virtual node only — never `nvme0n1`** (writing to it would destroy the
real disk). Verify with `lsblk -d -o NAME,SIZE,MODEL` / `dmesg` before any prep-write.

## Why it's blocked: no remote console
This is a consumer **ASUS PRIME X399-A** desktop — **no BMC/IPMI**, no serial console, and the user
has no physical access. The MOK Manager / UEFI screens run pre-boot (before SSH), so neither
enrolling the key nor disabling Secure Boot is reachable remotely. One brief physical-console action
by anyone unblocks all remaining work; after that, Stages A/B/C run entirely over SSH.

## How to resume (after the one-time console unlock)

**Pick ONE at the physical console:**
- **Disable Secure Boot in BIOS** (reboot → UEFI setup → Secure Boot = Disabled → save). Simplest;
  no signing needed afterward. *Or*
- **Enroll the MOK** (keeps Secure Boot): over SSH run
  `sudo mokutil --import /var/lib/shim-signed/mok/MOK.der` (set a password) → reboot → at the blue
  MOK Manager: *Enroll MOK → Continue → Yes →* type the password. Verify:
  `mokutil --test-key /var/lib/shim-signed/mok/MOK.der`.

**Then over SSH:**
```bash
cd /home/juchanlee/nvmevirt
sudo -v
./reload.sh                       # builds → signs → insmod
lsblk -d -o NAME,SIZE,MODEL       # identify the small virtual node (NOT nvme0n1)
# Stage A:
DEV=/dev/nvme1n1                  # ← VERIFY this is the ~3.6 GiB virtual device
sudo python3 nvmev-evaluation/common/set_perf.py max
cd nvmev-evaluation/fio
sudo DEV=$DEV RR_SIZE=2g fio workloads/rr-prep-write.fio
sudo DEV=$DEV RR_SIZE=2g RR_RUNTIME=30 fio workloads/rr-seq-read.fio
./../../notify.sh "Stage A done — baseline seq-read = <BW>, no errors."
# Then proceed to Stage B (implement reclaim), then Stage C (verify).
```

## Files changed/added this session
- `reload.sh` — drop the `E820_TYPE_RESERVED_KERN` define on 6.8; auto-sign module with MOK before
  `insmod`.
- `QUICKSTART.md` — host kernel updated to 6.8.0-111; device-node safety warning; kernel-version
  build note; Secure Boot / MOK signing section.
- `/etc/sudoers.d/nvmev-timeout` — added `Defaults !tty_tickets` (system file, not in repo).
- `/var/lib/shim-signed/mok/MOK.{key,der}` — signing keypair (outside repo; private key NOT
  committed).
- `SESSION_SUMMARY.md` — this file.
