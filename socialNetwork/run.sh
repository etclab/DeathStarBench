#!/bin/bash

# runs the benchmark for each strategies: istio, st2-NIChaRes, st3-TokRev, st4-AudUpd, st5-AttUpd

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="$SCRIPT_DIR/results/01-06-26"

STRATEGIES=("istio" "st2-NIChaRes" "st3-TokRev" "st4-AudUpd" "st5-AttUpd")
# RPS_VALUES=(500 750 1000 1250 1500 2000 3000 4000)
# RPS_VALUES=(1000 2000 4000 8000 12000 16000 24000 32000)
RPS_VALUES=(50 100 150 200 250 300 350 400 450 500)
DURATION=240

mkdir -p "$RESULTS_DIR"

for STRAT in "${STRATEGIES[@]}"; do
    RES_DIR="${RESULTS_DIR}/${STRAT}"
    mkdir -p "$RES_DIR"
    LOG_FILE="$RES_DIR/run.log"

    (
        exec > >(tee -a "$LOG_FILE") 2>&1

        echo "=== Benchmark run started at $(date) for $STRAT ==="

        for RPS in "${RPS_VALUES[@]}"; do
            echo "Running benchmark: STRAT=$STRAT, RPS=$RPS, DURATION=$DURATION"
            DURATION=$DURATION STRAT=$STRAT RPS=$RPS RES_DIR=$RES_DIR \
                $SCRIPT_DIR/setup_social_network.sh run-mixed-load
        done

        echo "=== Benchmark run completed at $(date) for $STRAT ==="
    )
done
