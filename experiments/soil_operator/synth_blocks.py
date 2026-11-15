"""Make TEST blocks for the soil-recovery test: one soil, several weeks IN A ROW.

synth.py gives every week its own soil. A real farm block keeps its soil all season,
and we get to watch it for many weeks (rain, dry spells, big irrigations).
So here each "block" gets:
  - one soil + roots (drawn exactly like synth.py, same regions)
  - W weeks in a row of ET (consecutive days, one scale for the block)
  - a new drip schedule each week (the farmer changes things)
  - week 1 starts from a random profile; every next week starts where the last one ended

These blocks are NOT for training. fit_soil.py uses them to ask: if we only see
a few probe depths for a few weeks, can we find the soil and then forecast the
next weeks at every depth?

Usage (CPU, like synth.py):
  python synth_blocks.py --blocks 300 --weeks 8 --out blocks_test.pt
"""
import argparse, os, time
import pandas as pd, torch
import torch_richards as TR
import synth as S


def block_et(regions, g, et_days, W):
    """(B, W, 168) ET in cm/h: W consecutive real weeks, one scale per block."""
    B = len(regions)
    start = torch.randint(0, len(et_days) - 7 * W, (B,), generator=g)
    days = torch.stack([et_days[s:s + 7 * W] for s in start])               # (B, 7W) mm/day
    lo = torch.tensor([0.7, 0.8, 0.5])[regions]
    hi = torch.tensor([1.3, 1.4, 1.6])[regions]
    days = days * (lo + (hi - lo) * torch.rand(B, generator=g))[:, None]
    hourly = (days[:, :, None] * S.daily_shape()[None, None, :]).reshape(B, W, S.HOURS)
    return hourly / 10


def make_blocks(args):
    """Make `n` blocks. Each block is solved ON ITS OWN, so one hard block (very sandy soil,
    heavy rain -> tiny time steps) can't slow down the others. A block that takes longer
    than `max_s` seconds is dropped, so a job can never hang for ages."""
    seed, n, W, max_s = args
    torch.set_num_threads(1)
    g = torch.Generator().manual_seed(seed)
    et_days = S.load_et_days()
    keep = {k: [] for k in ("water_in", "et", "params", "theta", "region")}
    dropped = 0
    for i in range(n):
        regions = S.random_regions(1, g)
        s, p = S.random_soils(regions, g)
        et = block_et(regions, g, et_days, W)
        wi = torch.stack([S.random_water_in(regions, g, et[:, k]) for k in range(W)], dim=1)
        th0 = S.random_start(s, 1, g)
        weeks, ok, t0 = [], True, time.time()
        for k in range(W):
            th, miss = S.simulate(s, p, wi[:, k], et[:, k], th0)
            ok = ok and bool(torch.isfinite(th).all()) and float(miss) < 0.05
            if not ok or time.time() - t0 > max_s:
                ok = False
                break
            weeks.append(th)
            th0 = th[:, -1]                               # next week starts where this one ended
        if not ok:
            dropped += 1
            continue
        th = torch.stack(weeks, dim=1)                    # (1, W, 168, 47)
        for k, v in (("water_in", wi), ("et", et), ("params", p), ("theta", th), ("region", regions)):
            keep[k].append(v.float() if k != "region" else v)
    if not keep["params"]:
        return None, dropped
    return {k: torch.cat(v) for k, v in keep.items()}, dropped


if __name__ == "__main__":
    from multiprocessing import Pool
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=300)
    ap.add_argument("--weeks", type=int, default=8)
    ap.add_argument("--chunk", type=int, default=4, help="blocks per job")
    ap.add_argument("--max_s", type=float, default=180, help="drop a block that takes longer than this (seconds)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="blocks_test.pt")
    a = ap.parse_args()

    jobs = [(a.seed * 100000 + j, min(a.chunk, a.blocks - j * a.chunk), a.weeks, a.max_s)
            for j in range((a.blocks + a.chunk - 1) // a.chunk)]
    print(f"{a.blocks} blocks x {a.weeks} weeks in {len(jobs)} jobs on {a.workers} CPU cores")
    parts, dropped, t0 = [], 0, time.time()
    with Pool(a.workers) as pool:
        for part, d in pool.imap_unordered(make_blocks, jobs):
            if part is not None:
                parts.append(part)
            dropped += d
            done = sum(len(p["params"]) for p in parts) + dropped
            print(f"{done}/{a.blocks} blocks ({dropped} dropped as broken), {time.time() - t0:.0f} s", flush=True)
    data = {k: torch.cat([p[k] for p in parts]) for k in parts[0]}
    data["z_cm"] = TR.Z.float()
    torch.save(data, a.out)
    counts = torch.bincount(data["region"], minlength=3).tolist()
    print("saved", a.out, {k: tuple(v.shape) for k, v in data.items()}, "sandy/loamy/wide:", counts)
