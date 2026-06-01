#!/usr/bin/env bash
# Max-bandwidth RRT time-series sweep (0602 follow-up). Same as rr_sweep_fine.sh
# but uses the libaio iodepth=16 read workload so reclaim dips show against the
# ~3357 MiB/s MAX-bandwidth baseline instead of the QD1/psync ~954. Writes
# distinct *_iod16 labels so neither the QD1 1s nor the 250ms logs are clobbered.
# Same region/duration as the QD1 sweep (RR_SIZE=512m, 180 s) for comparability.
#
#   ./rr_sweep_hiqd.sh        # OFF + RRT {16,64,256,1024} at iodepth=16, 250 ms
set -uo pipefail
cd "$(dirname "$0")"

export RR_SIZE="${RR_SIZE:-512m}"
export RR_RUNTIME="${RR_RUNTIME:-180}"
export MEMMAP_SIZE="${MEMMAP_SIZE:-16G}"
export LOG_AVG_MSEC="${LOG_AVG_MSEC:-250}"
export RR_FIO="workloads/rr-seq-read-iodepth.fio"
export IODEPTH="${IODEPTH:-16}"
RUNTIME="$RR_RUNTIME"

set_rrt () {
    sed -i -E "s/#define READ_RECLAIM_THRESHOLD \\(.*\\)/#define READ_RECLAIM_THRESHOLD ($1)/" ssd_config.h
    grep -E 'define READ_RECLAIM_THRESHOLD' ssd_config.h
}

echo "########## HIQD SWEEP baseline OFF (RRT=1e9) iodepth=$IODEPTH ##########"
set_rrt 1000000000
./rr_run.sh "baseline_off_16g_iod16" "$RUNTIME" || echo "RUN baseline returned $?"

for RRT in 16 64 256 1024; do
    echo "########## HIQD SWEEP RRT=$RRT iodepth=$IODEPTH ##########"
    set_rrt "$RRT"
    ./rr_run.sh "rrt${RRT}_16g_iod16" "$RUNTIME" || echo "RUN rrt${RRT} returned $?"
    echo "########## END RRT=$RRT ##########"
done
echo "HIQD_SWEEP_COMPLETE"
