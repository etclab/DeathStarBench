#!/bin/bash

# retries the benchmark for each strategies: istio, st2-NIChaRes, st3-TokRev, st4-AudUpd, st5-AttUpd

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="$SCRIPT_DIR/results/01-01-26"

# STRATEGIES=("istio" "st2-NIChaRes" "st3-TokRev" "st4-AudUpd" "st5-AttUpd")
STRATEGIES=("st4-AudUpd")
# RPS_VALUES=(500 750 1000 1250 1500 2000 3000 4000)
# RPS_VALUES=(1000 2000 4000 8000 12000 16000 24000 32000)
RPS_VALUES=(8000)
DURATION=240

mkdir -p "$RESULTS_DIR"

for STRAT in "${STRATEGIES[@]}"; do
    RES_DIR="${RESULTS_DIR}/${STRAT}"
    mkdir -p "$RES_DIR"
    LOG_FILE="$RES_DIR/run.log"

    (
        exec > >(tee -a "$LOG_FILE") 2>&1

        echo "=== Checking if we need to retry for $STRAT ==="

        for RPS in "${RPS_VALUES[@]}"; do
            OUT_FILE="$RES_DIR/${RPS}.txt"
            MAX_RETRIES=3
            RETRY_COUNT=0

            while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
                # Check if the output file exists
                if [ ! -f "$OUT_FILE" ]; then
                    echo "Output file $OUT_FILE does not exist, running benchmark..."
                    DURATION=$DURATION STRAT=$STRAT RPS=$RPS RES_DIR=$RES_DIR \
                        $SCRIPT_DIR/setup_social_network.sh run-mixed-load
                    continue
                fi

                # Parse total requests from the output file
                # Example: "238568 requests in 4.00m, 2.88GB read"
                TOTAL_REQUESTS=$(grep -oP '^\s*\K\d+(?= requests in)' "$OUT_FILE" | head -1)
                if [ -z "$TOTAL_REQUESTS" ] || [ "$TOTAL_REQUESTS" -eq 0 ]; then
                    echo "Could not parse total requests from $OUT_FILE, skipping..."
                    break
                fi

                # Parse socket errors: connect X, read Y, write Z, timeout W
                # Example: "Socket errors: connect 0, read 693, write 476, timeout 0"
                SOCKET_ERRORS=0
                if grep -q "Socket errors:" "$OUT_FILE"; then
                    CONNECT_ERR=$(grep "Socket errors:" "$OUT_FILE" | grep -oP 'connect \K\d+' || echo 0)
                    READ_ERR=$(grep "Socket errors:" "$OUT_FILE" | grep -oP 'read \K\d+' || echo 0)
                    WRITE_ERR=$(grep "Socket errors:" "$OUT_FILE" | grep -oP 'write \K\d+' || echo 0)
                    TIMEOUT_ERR=$(grep "Socket errors:" "$OUT_FILE" | grep -oP 'timeout \K\d+' || echo 0)
                    SOCKET_ERRORS=$((${CONNECT_ERR:-0} + ${READ_ERR:-0} + ${WRITE_ERR:-0} + ${TIMEOUT_ERR:-0}))
                fi

                # Parse non-2xx/3xx responses
                # Example: "Non-2xx or 3xx responses: 7158"
                NON_2XX_3XX=0
                if grep -q "Non-2xx or 3xx responses:" "$OUT_FILE"; then
                    NON_2XX_3XX=$(grep "Non-2xx or 3xx responses:" "$OUT_FILE" | grep -oP '\d+$' || echo 0)
                fi

                TOTAL_ERRORS=$((SOCKET_ERRORS + ${NON_2XX_3XX:-0}))

                # Calculate error percentage (using bc for floating point)
                ERROR_PERCENT=$(echo "scale=4; $TOTAL_ERRORS * 100 / $TOTAL_REQUESTS" | bc)

                echo "RPS=$RPS: Total requests=$TOTAL_REQUESTS, Socket errors=$SOCKET_ERRORS, Non-2xx/3xx=$NON_2XX_3XX, Total errors=$TOTAL_ERRORS (${ERROR_PERCENT}%)"

                # Check if error rate is less than 1%
                IS_SUCCESS=$(echo "$ERROR_PERCENT < 1" | bc)
                if [ "$IS_SUCCESS" -eq 1 ]; then
                    echo "RPS=$RPS: Success! Error rate ${ERROR_PERCENT}% is below 1% threshold."
                    break
                else
                    RETRY_COUNT=$((RETRY_COUNT + 1))
                    echo "RPS=$RPS: Failed! Error rate ${ERROR_PERCENT}% exceeds 1% threshold. Retry $RETRY_COUNT of $MAX_RETRIES."

                    if [ $RETRY_COUNT -lt $MAX_RETRIES ]; then
                        # Rename the failed output file with timestamp
                        TIMESTAMP=$(date +%Y%m%d_%H%M%S)
                        FAILED_FILE="${OUT_FILE%.txt}.failed_${TIMESTAMP}.txt"
                        mv "$OUT_FILE" "$FAILED_FILE"
                        echo "Renamed $OUT_FILE to $FAILED_FILE"

                        # Retry the benchmark
                        echo "Retrying benchmark: STRAT=$STRAT, RPS=$RPS, DURATION=$DURATION"
                        DURATION=$DURATION STRAT=$STRAT RPS=$RPS RES_DIR=$RES_DIR \
                            $SCRIPT_DIR/setup_social_network.sh run-mixed-load
                    else
                        echo "RPS=$RPS: Max retries ($MAX_RETRIES) reached. Giving up."
                    fi
                fi
            done
        done
    )
done
