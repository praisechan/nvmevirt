#!/usr/bin/env bash
# 0602 §1B — bandwidth saturation sweep (SEPARATE from the reclaim test).
# Reclaim OFF, reload at 16G, pin OUR node by LOAD-ORDER (mirrors rr_run.sh's
# exact pin + cross-checks -- NOT a size check), prep-write an 8 GiB region that
# spans all 4 FTL instances, then sweep concurrency with libaio:
#   iodepth in {1,4,16,32,64} at numjobs=1, then numjobs in {2,4} at iodepth=32.
# Records aggregate read BW per point to rr_results/bw_saturation.csv.
#
#   ./bw_saturation.sh
# Requires a live sudo ticket. Leaves RRT as set here (1e9); caller restores 8.
set -uo pipefail
cd "$(dirname "$0")"
OUTDIR="rr_results"; mkdir -p "$OUTDIR"
CSV="$OUTDIR/bw_saturation.csv"

if ! sudo -n true 2>/dev/null; then echo "SUDO_EXPIRED — run: sudo -v"; exit 2; fi

MEMMAP_SIZE="${MEMMAP_SIZE:-16G}"
OUR_MEMMAP_START_BYTES=68719476736   # 64G offset
BW_SIZE_TOTAL="${BW_SIZE_TOTAL:-8g}"
BW_RUNTIME="${BW_RUNTIME:-30}"

# Reclaim OFF for this probe so it never interferes with throughput.
sed -i -E "s/#define READ_RECLAIM_THRESHOLD \\(.*\\)/#define READ_RECLAIM_THRESHOLD (1000000000)/" ssd_config.h
grep -E 'define READ_RECLAIM_THRESHOLD' ssd_config.h

# ---- reload + LOAD-ORDER device pin (mirrors rr_run.sh exactly) ----
sudo rmmod nvmev_rr 2>/dev/null || true
before=$(ls /dev/nvme*n1 2>/dev/null | sort)
echo "==> [bwsat] reload (MEMMAP_SIZE=$MEMMAP_SIZE, reclaim OFF)"
MEMMAP_START=64G MEMMAP_SIZE=$MEMMAP_SIZE CPUS=22,23 ./reload.sh >/tmp/bwsat_reload.log 2>&1
udevadm settle 2>/dev/null || sleep 1
after=$(ls /dev/nvme*n1 2>/dev/null | sort)
DEV=$(comm -13 <(echo "$before") <(echo "$after"))
if [ ! -d /proc/nvmev_rr ]; then echo "ABORT: nvmev_rr not loaded"; lsblk -d -o NAME,SIZE,MODEL; exit 1; fi
ms=$(cat /sys/module/nvmev_rr/parameters/memmap_start 2>/dev/null || echo "")
if [ "$ms" != "$OUR_MEMMAP_START_BYTES" ]; then echo "ABORT: memmap_start=$ms != $OUR_MEMMAP_START_BYTES"; exit 1; fi
if [ -z "$DEV" ] || [ "$(echo "$DEV" | wc -l)" -ne 1 ] || [ "$DEV" = "/dev/nvme0n1" ]; then
    echo "ABORT: ambiguous/empty DEV='$DEV'"; echo " before=[$before]"; echo " after=[$after]"; lsblk -d -o NAME,SIZE,MODEL; exit 1
fi
echo "==> [bwsat] device = $DEV (newly appeared across insmod; /proc/nvmev_rr present, memmap_start=64G)"

sudo dmesg -C
sudo python3 nvmev-evaluation/common/set_perf_rr.py max >/dev/null 2>&1 || true

cd nvmev-evaluation/fio
echo "==> [bwsat] prep-write $BW_SIZE_TOTAL (spans all 4 FTL instances)"
sudo DEV=$DEV RR_SIZE=$BW_SIZE_TOTAL fio workloads/rr-prep-write.fio >/tmp/bwsat_prep.log 2>&1
grep -E 'WRITE:' /tmp/bwsat_prep.log || true

echo "mode,iodepth,numjobs,bw_MiBps,bw_MBps,iops" > "../../$CSV"

# Parse "READ: bw=NNN.NMiB/s (NNN.NMB/s)..." + IOPS from a fio group report.
run_point () {
    local mode="$1" id="$2" nj="$3" size="$4" off="$5" tag="$6"
    local log="/tmp/bwsat_${tag}.log"
    echo "==> [bwsat] $mode iodepth=$id numjobs=$nj size=$size off_inc=$off"
    sudo DEV=$DEV BW_SIZE=$size BW_RUNTIME=$BW_RUNTIME IODEPTH=$id NUMJOBS=$nj \
        OFFSET_INCREMENT=$off fio workloads/seq-read-bw.fio >"$log" 2>&1
    local line; line=$(grep -E 'READ: bw=' "$log" | head -1)
    local mib mb iops
    mib=$(echo "$line" | grep -oE 'bw=[0-9.]+MiB/s' | grep -oE '[0-9.]+' | head -1)
    mb=$(echo "$line" | grep -oE '\([0-9.]+MB/s' | grep -oE '[0-9.]+' | head -1)
    iops=$(grep -E 'IOPS=' "$log" | head -1 | grep -oE 'IOPS=[0-9.k]+' | head -1 | sed 's/IOPS=//')
    mib=${mib:-NA}; mb=${mb:-NA}; iops=${iops:-NA}
    echo "    -> bw=${mib} MiB/s (${mb} MB/s) IOPS=${iops}"
    echo "$mode,$id,$nj,$mib,$mb,$iops" >> "../../$CSV"
}

# (1) iodepth sweep, numjobs=1, single job reads the whole 8 GiB region.
for ID in 1 4 16 32 64; do
    run_point "iodepth" "$ID" 1 "$BW_SIZE_TOTAL" 0 "id${ID}_nj1"
done

# (2) numjobs sweep at iodepth=32, tiling the 8 GiB across disjoint slices.
#     8g/2 = 4g, 8g/4 = 2g (offset_increment == per-job size => no overlap).
run_point "numjobs" 32 2 "4g" "4g" "id32_nj2"
run_point "numjobs" 32 4 "2g" "2g" "id32_nj4"

cd ../..
echo "==> [bwsat] CSV: $CSV"
cat "$CSV"
echo "BWSAT_COMPLETE"
