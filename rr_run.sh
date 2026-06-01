#!/usr/bin/env bash
# Stage C runner for the read-reclaim experiment.
# Usage: ./rr_run.sh <label> <runtime_sec>
#   Rebuilds+reloads nvmev_rr (picking up the current READ_RECLAIM_THRESHOLD),
#   pins OUR device by identity (3.7 GiB + /proc/nvmev_rr), applies max perf,
#   prep-writes, then loops a sequential read for <runtime_sec>, and reports
#   read-reclaim dmesg events + bandwidth. Requires a live sudo ticket.
set -euo pipefail
cd "$(dirname "$0")"

LABEL="${1:?usage: rr_run.sh <label> <runtime_sec>}"
RUNTIME="${2:?usage: rr_run.sh <label> <runtime_sec>}"
OUTDIR="rr_results"
mkdir -p "$OUTDIR"

if ! sudo -n true 2>/dev/null; then echo "SUDO_EXPIRED — run: sudo -v"; exit 2; fi

# Device size grew to ~16 GiB (memmap=16G$64G). At 16 GiB our LOGICAL size (~15 GiB) is about the
# SAME as the other user's ~15 GiB device and both share model CSL_Virt_MN_01 -- so size+model can
# NO LONGER tell our node apart from theirs. Pin OUR node by LOAD-ORDER instead (§2.3): the node
# that newly appears across our rmmod->insmod is ours, cross-checked by /proc/nvmev_rr + memmap_start.
MEMMAP_SIZE="${MEMMAP_SIZE:-16G}"
OUR_MEMMAP_START_BYTES=68719476736   # 64G offset == /sys/module/nvmev_rr/parameters/memmap_start

# Guarantee a clean slate: explicitly drop OUR module if a prior load is resident
# (only ever nvmev_rr -- never the other user's nvmev). reload.sh also tries, but
# this makes a stale load impossible to collide with the fresh insmod.
sudo rmmod nvmev_rr 2>/dev/null || true

# Snapshot existing nvme namespaces AFTER dropping ours, BEFORE insmod.
before=$(ls /dev/nvme*n1 2>/dev/null | sort)

echo "==> [$LABEL] reload (MEMMAP_SIZE=$MEMMAP_SIZE, rebuild picks up RRT=$(grep -E 'define READ_RECLAIM_THRESHOLD' ssd_config.h | grep -oE '[0-9]+'))"
MEMMAP_START=64G MEMMAP_SIZE=$MEMMAP_SIZE CPUS=22,23 ./reload.sh >/tmp/rr_reload_$LABEL.log 2>&1
tail -1 /tmp/rr_reload_$LABEL.log >/dev/null

# Let the new namespace settle, then diff: the newly-appeared node == ours.
udevadm settle 2>/dev/null || sleep 1
after=$(ls /dev/nvme*n1 2>/dev/null | sort)
DEV=$(comm -13 <(echo "$before") <(echo "$after"))

# --- Hard cross-checks (ALL must hold) — never the 1.8T real disk or a 15G other-user node ---
if [ ! -d /proc/nvmev_rr ]; then
    echo "ABORT: nvmev_rr not loaded"; lsblk -d -o NAME,SIZE,MODEL; exit 1
fi
ms=$(cat /sys/module/nvmev_rr/parameters/memmap_start 2>/dev/null || echo "")
if [ "$ms" != "$OUR_MEMMAP_START_BYTES" ]; then
    echo "ABORT: nvmev_rr memmap_start=$ms != $OUR_MEMMAP_START_BYTES (not our 64G region)"; exit 1
fi
# DEV must be exactly one newly-appeared node, and never the real disk.
if [ -z "$DEV" ] || [ "$(echo "$DEV" | wc -l)" -ne 1 ] || [ "$DEV" = "/dev/nvme0n1" ]; then
    echo "ABORT: ambiguous/empty DEV='$DEV' (refusing real disk / multiple nodes)"
    echo "  before=[$before]"; echo "  after=[$after]"; lsblk -d -o NAME,SIZE,MODEL; exit 1
fi
echo "==> [$LABEL] device = $DEV (newly appeared across insmod; /proc/nvmev_rr present, memmap_start=64G)"

# clear dmesg marker so we count only this run's reclaims
sudo dmesg -C
sudo python3 nvmev-evaluation/common/set_perf_rr.py max >/dev/null 2>&1 || true

RR_SIZE="${RR_SIZE:-512m}"
cd nvmev-evaluation/fio
echo "==> [$LABEL] STEP 1 prep-write $RR_SIZE"
sudo DEV=$DEV RR_SIZE=$RR_SIZE fio workloads/rr-prep-write.fio >/tmp/rr_prep_$LABEL.log 2>&1
grep -E 'WRITE:' /tmp/rr_prep_$LABEL.log || true

echo "==> [$LABEL] STEP 2 seq-read loop ${RUNTIME}s (RR_SIZE=$RR_SIZE)"
sudo DEV=$DEV RR_SIZE=$RR_SIZE RR_RUNTIME=$RUNTIME fio workloads/rr-seq-read.fio >"../../$OUTDIR/fio_$LABEL.log" 2>&1
grep -E 'READ:|IOPS' "../../$OUTDIR/fio_$LABEL.log" | head
cp -f rr-read_bw.1.log "../../$OUTDIR/bw_$LABEL.log" 2>/dev/null || true
cd ../..

RECLAIM_LINES=$(sudo dmesg | grep -c 'read-reclaim:' || true)
# Authoritative count: the cumulative per-instance counter (total_reclaims=N) is not
# subject to dmesg ring-buffer wrap the way a line-count is. Report the MAX surviving
# value (final count for the leading FTL instance); all 4 instances are ~symmetric, so
# a whole-device estimate ~= 4x this. dmesg may have wrapped if RECLAIM_LINES is huge.
MAX_TOTAL=$(sudo dmesg | grep -oE 'total_reclaims=[0-9]+' | grep -oE '[0-9]+' | sort -n | tail -1 || true)
MAX_TOTAL=${MAX_TOTAL:-0}
echo "==> [$LABEL] read-reclaim: dmesg lines=$RECLAIM_LINES, max total_reclaims/instance=$MAX_TOTAL (whole-device est ~$((MAX_TOTAL*4)))"
sudo dmesg | grep 'read-reclaim:' | tail -3 || true
echo "==> [$LABEL] DONE. logs: $OUTDIR/fio_$LABEL.log, $OUTDIR/bw_$LABEL.log"
