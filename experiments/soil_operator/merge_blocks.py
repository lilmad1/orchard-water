"""Add block weeks (from synth_blocks.py) to a normal training file (from synth.py).

Why: synth.py starts every week from a simple profile (two flat layers). In a real
season, a week starts where the last one ended: wet fronts, dry tops, curved
profiles. The FNO never saw those, so it drifts when run week after week.
Block weeks give it exactly those realistic starts.

Usage:
  python merge_blocks.py --synth synth_20k.pt --blocks blocks_train.pt --out synth_mix.pt
"""
import argparse
import torch

ap = argparse.ArgumentParser()
ap.add_argument("--synth", default="synth_20k.pt")
ap.add_argument("--blocks", default="blocks_train.pt")
ap.add_argument("--out", default="synth_mix.pt")
a = ap.parse_args()

s, b = torch.load(a.synth), torch.load(a.blocks)
W = b["theta"].shape[1]
flat = {
    "water_in": b["water_in"].flatten(0, 1),
    "et": b["et"].flatten(0, 1),
    "params": b["params"].repeat_interleave(W, 0),
    "theta": b["theta"].flatten(0, 1),
    "region": b["region"].repeat_interleave(W, 0),
}
out = {k: torch.cat([s[k], flat[k]]) for k in flat}
out["z_cm"] = s["z_cm"]
torch.save(out, a.out)
print(f"saved {a.out}: {len(s['params'])} normal weeks + {len(flat['params'])} block weeks = {len(out['params'])}")
