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

# Guarantee a clean slate: explicitly drop OUR module if a prior load is resident
# (only ever nvmev_rr -- never the other user's nvmev). reload.sh also tries, but
# this makes a stale load impossible to collide with the fresh insmod.
sudo rmmod nvmev_rr 2>/dev/null || true

echo "==> [$LABEL] reload (rebuild picks up RRT=$(grep -E 'define READ_RECLAIM_THRESHOLD' ssd_config.h | grep -oE '[0-9]+'))"
MEMMAP_START=64G MEMMAP_SIZE=4G CPUS=22,23 ./reload.sh >/tmp/rr_reload_$LABEL.log 2>&1
tail -1 /tmp/rr_reload_$LABEL.log >/dev/null

# --- pin OUR device by identity (never the 1.8T real disk or a 15G other-user node) ---
DEV=""
for d in /sys/block/nvme*n1; do
    n=$(basename "$d")
    mib=$(( $(cat "$d/size") * 512 / 1024 / 1024 ))
    if [ "$mib" -eq 3827 ]; then DEV="/dev/$n"; fi
done
if [ -z "$DEV" ] || [ ! -d /proc/nvmev_rr ]; then
    echo "GUARD_FAIL: could not find OUR 3827 MiB nvmev_rr node"; lsblk -d -o NAME,SIZE,MODEL; exit 1
fi
echo "==> [$LABEL] device = $DEV (verified 3827 MiB, /proc/nvmev_rr present)"

# clear dmesg marker so we count only this run's reclaims
sudo dmesg -C
sudo python3 nvmev-evaluation/common/set_perf_rr.py max >/dev/null 2>&1 || true

cd nvmev-evaluation/fio
echo "==> [$LABEL] STEP 1 prep-write 2g"
sudo DEV=$DEV RR_SIZE=2g fio workloads/rr-prep-write.fio >/tmp/rr_prep_$LABEL.log 2>&1
grep -E 'WRITE:' /tmp/rr_prep_$LABEL.log || true

echo "==> [$LABEL] STEP 2 seq-read loop ${RUNTIME}s"
sudo DEV=$DEV RR_SIZE=2g RR_RUNTIME=$RUNTIME fio workloads/rr-seq-read.fio >"../../$OUTDIR/fio_$LABEL.log" 2>&1
grep -E 'READ:|IOPS' "../../$OUTDIR/fio_$LABEL.log" | head
cp -f rr-read_bw.1.log "../../$OUTDIR/bw_$LABEL.log" 2>/dev/null || true
cd ../..

RECLAIMS=$(sudo dmesg | grep -c 'read-reclaim:' || true)
echo "==> [$LABEL] read-reclaim events in dmesg: $RECLAIMS"
sudo dmesg | grep 'read-reclaim:' | tail -3 || true
echo "==> [$LABEL] DONE. logs: $OUTDIR/fio_$LABEL.log, $OUTDIR/bw_$LABEL.log"
