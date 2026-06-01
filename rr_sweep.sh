#!/usr/bin/env bash
# RRT sweep driver: for each threshold, patch ssd_config.h and run rr_run.sh
# (rebuild+reload+prep+180s read+report). Reclaim ON. Baseline (RRT=1e9) ran separately.
set -uo pipefail
cd "$(dirname "$0")"
RUNTIME="${RR_RUNTIME:-180}"
export RR_SIZE="${RR_SIZE:-512m}"
export MEMMAP_SIZE="${MEMMAP_SIZE:-16G}"

for RRT in 16 64 256 1024; do
    echo "########## SWEEP RRT=$RRT ##########"
    sed -i -E "s/#define READ_RECLAIM_THRESHOLD \\(.*\\)/#define READ_RECLAIM_THRESHOLD ($RRT)/" ssd_config.h
    grep -E 'define READ_RECLAIM_THRESHOLD' ssd_config.h
    ./rr_run.sh "rrt${RRT}_16g" "$RUNTIME" || echo "RUN rrt${RRT} returned $?"
    echo "########## END RRT=$RRT ##########"
done
echo "SWEEP_COMPLETE"
