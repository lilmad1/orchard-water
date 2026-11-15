#!/usr/bin/env bash
# Tiny end-to-end run on CPU (a few minutes). Proves the scripts run; the FNO it trains
# is NOT usable (too little data, too few epochs). Run from this folder.
set -euo pipefail
OUT=${1:-./_out}; mkdir -p "$OUT"
python synth.py --n 48 --chunk 8 --batch 4 --workers 2 --out "$OUT/synth_train.pt"
python train_fno.py --data "$OUT/synth_train.pt" --epochs 3 --batch 8 --out "$OUT/fno.pt"
python check_fno.py --data "$OUT/synth_train.pt" --model "$OUT/fno.pt"
