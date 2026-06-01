#!/usr/bin/env bash
# Fine-grained RRT sweep for the 0602 time-series viz: same workload as the 0601
# sweep (RR_SIZE=512m, 180 s, set_perf_rr max, prep-write each point) but with a
# finer BW-log resolution (LOG_AVG_MSEC=250) so reclaim bursts render smooth, not
# aliased. Writes distinct *_250ms labels so the 0601 1 s logs are NOT clobbered.
# Includes the reclaim-OFF baseline. Reuses rr_run.sh (load-order device pin).
#
#   ./rr_sweep_fine.sh        # OFF + RRT {16,64,256,1024} at 250 ms
set -uo pipefail
cd "$(dirname "$0")"

export RR_SIZE="${RR_SIZE:-512m}"
export RR_RUNTIME="${RR_RUNTIME:-180}"
export MEMMAP_SIZE="${MEMMAP_SIZE:-16G}"
export LOG_AVG_MSEC="${LOG_AVG_MSEC:-250}"
RUNTIME="$RR_RUNTIME"

set_rrt () {
    sed -i -E "s/#define READ_RECLAIM_THRESHOLD \\(.*\\)/#define READ_RECLAIM_THRESHOLD ($1)/" ssd_config.h
    grep -E 'define READ_RECLAIM_THRESHOLD' ssd_config.h
}

echo "########## FINE SWEEP baseline OFF (RRT=1e9) @ ${LOG_AVG_MSEC}ms ##########"
set_rrt 1000000000
./rr_run.sh "baseline_off_16g_250ms" "$RUNTIME" || echo "RUN baseline returned $?"

for RRT in 16 64 256 1024; do
    echo "########## FINE SWEEP RRT=$RRT @ ${LOG_AVG_MSEC}ms ##########"
    set_rrt "$RRT"
    ./rr_run.sh "rrt${RRT}_16g_250ms" "$RUNTIME" || echo "RUN rrt${RRT} returned $?"
    echo "########## END RRT=$RRT ##########"
done
echo "FINE_SWEEP_COMPLETE"
