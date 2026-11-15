"""BEFORE vs AFTER test data: the same field (same soil, same roots) in two seasons.

  season 1 = BEFORE: the farmer waters the way he always did (synth.py amounts)
  season 2 = AFTER:  a different weather year, and the farmer uses 60-90 % of his usual water

Each season is W weeks in a row (first half = warm-up, second half scored), solved by
our physics, so we know the TRUE drainage and plant use in both seasons.
fit_pairs.py then asks: how well does the model get the SAVED water (before minus after),
and which part of it was "plants used less" vs "less drained away"?

Note: the 60-90 % cut is applied to all water in (drip + rain); rain is a small share here.

Usage (CPU, like synth_blocks.py):
  python synth_pairs.py --blocks 120 --weeks 12 --out pairs.pt
"""
import argparse, os, time
import pandas as pd, torch
import torch_richards as TR
import synth as S
from synth_blocks import block_et


def make_pairs(args):
    seed, n, W, max_s = args
    torch.set_num_threads(1)
    g = torch.Generator().manual_seed(seed)
    et_days = S.load_et_days()
    keep = {k: [] for k in ("water_in", "et", "params", "theta", "region", "cut")}
    dropped = 0
    for i in range(n):
        regions = S.random_regions(1, g)
        s, p = S.random_soils(regions, g)
        cut = 0.6 + 0.3 * torch.rand(1, generator=g)                  # after = 60-90 % of before
        ok, t0, seasons_wi, seasons_et, seasons_th = True, time.time(), [], [], []
        for season in range(2):
            et = block_et(regions, g, et_days, W)                       # (1, W, 168), new weather each season
            wi = torch.stack([S.random_water_in(regions, g, et[:, k]) for k in range(W)], dim=1)
            if season == 1:
                wi = wi * cut[:, None, None]
            th0 = S.random_start(s, 1, g)
            weeks = []
            for k in range(W):
                th, miss = S.simulate(s, p, wi[:, k], et[:, k], th0)
                ok = ok and bool(torch.isfinite(th).all()) and float(miss) < 0.05 and time.time() - t0 < max_s
                if not ok:
                    break
                weeks.append(th)
                th0 = th[:, -1]
            if not ok:
                break
            seasons_wi.append(wi)
            seasons_et.append(et)
            seasons_th.append(torch.stack(weeks, dim=1))
        if not ok:
            dropped += 1
            continue
        keep["water_in"].append(torch.stack(seasons_wi, 1).float())     # (1, 2, W, 168)
        keep["et"].append(torch.stack(seasons_et, 1).float())
        keep["theta"].append(torch.stack(seasons_th, 1).float())        # (1, 2, W, 168, 47)
        keep["params"].append(p.float())
        keep["region"].append(regions)
        keep["cut"].append(cut.float())
    if not keep["params"]:
        return None, dropped
    return {k: torch.cat(v) for k, v in keep.items()}, dropped


if __name__ == "__main__":
    from multiprocessing import Pool
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=120)
    ap.add_argument("--weeks", type=int, default=12)
    ap.add_argument("--chunk", type=int, default=4)
    ap.add_argument("--max_s", type=float, default=300, help="drop a field that takes longer than this (seconds)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--seed", type=int, default=31)
    ap.add_argument("--out", default="pairs.pt")
    a = ap.parse_args()
    jobs = [(a.seed * 100000 + j, min(a.chunk, a.blocks - j * a.chunk), a.weeks, a.max_s)
            for j in range((a.blocks + a.chunk - 1) // a.chunk)]
    print(f"{a.blocks} fields x 2 seasons x {a.weeks} weeks in {len(jobs)} jobs on {a.workers} CPU cores")
    parts, dropped, t0 = [], 0, time.time()
    with Pool(a.workers) as pool:
        for part, d in pool.imap_unordered(make_pairs, jobs):
            if part is not None:
                parts.append(part)
            dropped += d
            done = sum(len(p["params"]) for p in parts) + dropped
            print(f"{done}/{a.blocks} fields ({dropped} dropped), {time.time() - t0:.0f} s", flush=True)
    data = {k: torch.cat([p[k] for p in parts]) for k in parts[0]}
    data["z_cm"] = TR.Z.float()
    torch.save(data, a.out)
    print("saved", a.out, {k: tuple(v.shape) for k, v in data.items()})
