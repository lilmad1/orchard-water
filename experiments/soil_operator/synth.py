"""Make synthetic training data for the FNO: run our soil physics many times.

Each sample is one week (168 hours) of one made-up drip-irrigated citrus block,
from one of three made-up "regimes":

  regime 0 = sandy:  sandy soils, growers often water MORE than the trees use
  regime 1 = loamy:  loamy, heavier soils, water quotas, often LESS than the trees use
  regime 2 = wide:   any soil inside wide limits, any schedule (so odd farms don't surprise it)

For each sample we draw:
  - a soil (2 layers) from that region's soil types, each a bit varied
  - roots (share f of ET, depth lam)
  - a start profile
  - daily ET (synthetic seasonal series by default, or your own CSV, see load_et_days), shaped over the day (more at noon)
  - a drip schedule tied to the trees' water use, plus rain typical for the region

The physics then gives the water content at all 47 depths, every hour.
That is the "answer" the FNO must learn to copy.

Usage:  python synth.py --n 3000 --out synth_train.pt
"""
import argparse, math, os, time
import numpy as np, pandas as pd, torch
import torch_richards as TR

HOURS = 168
REGION_SHARE = {0: 0.35, 1: 0.45, 2: 0.20}   # sandy, loamy, wide

# Soil types: van Genuchten class averages from Rosetta (Schaap et al. 2001):
#   tr, ts, log10 a (1/cm), n, log10 ks (cm/h)
SOIL_TYPES = {
    "sand":            (0.053, 0.375, -1.453, 3.18, 1.43),
    "loamy sand":      (0.049, 0.390, -1.460, 1.75, 0.64),
    "sandy loam":      (0.039, 0.387, -1.574, 1.45, 0.20),
    "loam":            (0.061, 0.399, -1.954, 1.47, -0.30),
    "sandy clay loam": (0.063, 0.384, -1.676, 1.33, -0.26),
    "clay loam":       (0.079, 0.442, -1.801, 1.41, -0.47),
    "silt loam":       (0.065, 0.439, -2.291, 1.66, -0.12),
}
# Which soil types each regime gets, and how often. These mixes are ESTIMATES (assumptions),
# not fitted to any real site.
REGION_SOILS = {
    0: {"sand": 0.35, "loamy sand": 0.45, "sandy loam": 0.20},
    1: {"loam": 0.35, "clay loam": 0.25, "sandy clay loam": 0.15, "sandy loam": 0.15, "silt loam": 0.10},
}
# Wide limits (region 2, and the FNO's input scaling): tr, ts, log10 a, n, log10 ks
WIDE_LO = (0.0, 0.30, -2.7, 1.2, -1.5)
WIDE_HI = (0.10, 0.50, -1.0, 3.5, 1.6)
SPREAD = (0.01, 0.03, 0.15, 0.12, 0.30)   # how much a real soil varies around its class average


def load_et_days(path=None):
    """Daily ET (mm/day) as a 1-D tensor, at least a few weeks long.

    Default: a synthetic year (seasonal curve + weather noise, fixed seed), so nothing
    needs to be downloaded. To use your own series instead, give a CSV path (or set the
    environment variable ORCHARDWATER_ET_CSV) with one row per day and a column `et_mm_day`.
    """
    path = path or os.environ.get("ORCHARDWATER_ET_CSV")
    if path:
        v = pd.read_csv(path)["et_mm_day"].interpolate().values
        return torch.tensor(v, dtype=torch.float64)
    rng = np.random.default_rng(12345)
    doy = np.arange(365)
    seasonal = 3.0 + 2.2 * np.sin(2 * np.pi * (doy - 100) / 365)
    noise = np.zeros(365)
    for i in range(1, 365):                      # AR(1) weather noise
        noise[i] = 0.7 * noise[i - 1] + rng.normal(0, 0.5)
    return torch.tensor(np.clip(seasonal + noise, 0.3, 8.0), dtype=torch.float64)


def random_regions(B, g):
    probs = torch.tensor([REGION_SHARE[r] for r in range(3)])
    return torch.multinomial(probs, B, replacement=True, generator=g)


def random_soils(regions, g):
    """One soil (2 layers) per sample. Returns soil arrays for the solver and the 12 numbers (B, 12)."""
    B = len(regions)
    lo, hi, sp = torch.tensor(WIDE_LO), torch.tensor(WIDE_HI), torch.tensor(SPREAD)
    layers_p = []
    for k in range(2):
        p = lo + (hi - lo) * torch.rand(B, 5, generator=g)          # wide by default
        for r, mix in REGION_SOILS.items():
            idx = torch.nonzero(regions == r).flatten()
            if len(idx) == 0:
                continue
            names = list(mix)
            pick = torch.multinomial(torch.tensor([mix[n] for n in names]), len(idx), replacement=True, generator=g)
            base = torch.tensor([SOIL_TYPES[names[i]] for i in pick.tolist()])
            noise = sp * torch.randn(len(idx), 5, generator=g)
            p[idx] = torch.minimum(torch.maximum(base + noise, lo), hi)
        layers_p.append(p)
    f = 0.3 + 0.7 * torch.rand(B, generator=g)            # roots take 30-100 % of ET
    lam = 10 + 50 * torch.rand(B, generator=g)             # root depth scale 10-60 cm
    params = torch.cat(layers_p + [f[:, None], lam[:, None]], dim=1)   # (B, 12)
    layers = []
    for k in range(2):
        tr, ts, la, n, lks = params[:, 5 * k:5 * k + 5].T
        layers.append(dict(tr=tr[:, None], ts=ts[:, None], a=10 ** la[:, None], n=n[:, None], ks=10 ** lks[:, None]))
    return TR.soil_arrays(layers), params


def daily_shape():
    """How one day's ET is spread over 24 hours: zero at night, peak at 13:00."""
    hr = torch.arange(24.0)
    w = torch.clamp(torch.sin(math.pi * (hr - 6) / 14), min=0)   # daylight 6:00-20:00
    return w / w.sum()


def random_et(regions, g, et_days):
    """(B, HOURS) ET in cm/h from random 7-day stretches of the daily ET series.
    sandy: 0.7-1.3x. loamy: 0.8-1.4x (estimate). Wide: 0.5-1.6x."""
    B = len(regions)
    start = torch.randint(0, len(et_days) - 7, (B,), generator=g)
    days = torch.stack([et_days[s:s + 7] for s in start])                  # mm/day
    lo = torch.tensor([0.7, 0.8, 0.5])[regions]
    hi = torch.tensor([1.3, 1.4, 1.6])[regions]
    days = days * (lo + (hi - lo) * torch.rand(B, generator=g))[:, None]
    hourly = (days[:, :, None] * daily_shape()[None, None, :]).reshape(B, HOURS)
    return hourly / 10                                                      # mm -> cm


def random_water_in(regions, g, et):
    """(B, HOURS) water arriving at the top in cm/h: drip + rain.

    How much drip: a factor x the week's tree water use.
      sandy  0.8-2.2x (over-watering)
      loamy  0.5-1.1x (quotas and deficit irrigation)
      wide   0.3-2.5x
    How often: sandy mostly 1 long pulse a day or every 2nd day; loamy often 1-3 short
    pulses every day (high-frequency drip); wide anything.
    Rain: sandy 20 % of weeks 2-22 mm; loamy 15 % of weeks 2-40 mm (rare heavy storms).
    All of these are assumptions, not measurements.
    """
    B = len(regions)
    w = torch.zeros(B, HOURS)
    week_use = et.sum(1)                                                    # cm
    lo = torch.tensor([0.8, 0.5, 0.3])[regions]
    hi = torch.tensor([2.2, 1.1, 2.5])[regions]
    total = week_use * (lo + (hi - lo) * torch.rand(B, generator=g))
    for b in range(B):
        r = int(regions[b])
        if r == 0:
            every, pulses, length = (1 if torch.rand(1, generator=g) < 0.6 else 2), 1, int(torch.randint(2, 6, (1,), generator=g))
        elif r == 1:
            every, pulses, length = 1, int(torch.randint(1, 4, (1,), generator=g)), int(torch.randint(1, 3, (1,), generator=g))
        else:
            every, pulses, length = int(torch.randint(1, 4, (1,), generator=g)), int(torch.randint(1, 4, (1,), generator=g)), int(torch.randint(1, 5, (1,), generator=g))
        days = list(range(0, 7, every))
        per_pulse = total[b] / (len(days) * pulses * length)               # cm/h during a pulse
        for d in days:
            for k in range(pulses):
                t0 = d * 24 + 6 + k * 5 + int(torch.randint(0, 3, (1,), generator=g))
                w[b, t0:t0 + length] += per_pulse
    p_rain = torch.tensor([0.20, 0.15, 0.20])[regions]
    max_mm = torch.tensor([2.0, 3.8, 3.0])[regions]                        # cm above the 0.2 minimum
    rainy = torch.rand(B, generator=g) < p_rain
    t_rain = torch.randint(0, HOURS - 6, (B,), generator=g)
    cm = 0.2 + max_mm * torch.rand(B, generator=g)
    for b in torch.nonzero(rainy).flatten().tolist():
        w[b, t_rain[b]:t_rain[b] + 6] += cm[b] / 6
    return w


def random_start(s, B, g):
    """Random start profile: each layer somewhere between dry and fairly wet."""
    frac = 0.05 + 0.5 * torch.rand(B, 2, generator=g)
    top = TR.Z < TR.SPLIT_CM
    fr = torch.where(top, frac[:, :1], frac[:, 1:])
    return s["tr"] + (s["ts"] - s["tr"]) * fr


DZW = torch.full((TR.N,), TR.DZ)
DZW[0] = DZW[-1] = TR.DZ / 2   # the top and bottom slices are half as thick


def storage(s, h):
    """Water stored in the column (cm of water)."""
    return (TR.theta(s, h) * DZW).sum(-1)


@torch.no_grad()
def simulate(s, p, water_in, et, theta0, iters=6, tol_cm=1e-3, h50=-1500.0, return_drain=False):
    """Run the physics with water coming in at the top, one hour at a time.

    Water entering dry sand fast is hard for the solver: with a big time step it
    loses or invents water. So inside each hour we take small steps and check
    the water balance after each one (storage change = water in - drainage - roots).
    If any sample is off by more than tol_cm, the step is halved and redone.
    Returns theta (B, HOURS, N), the total water-balance miss per sample (B,)
    in cm, and, if asked, total drainage (B,) in cm.
    """
    w = torch.stack([TR.root_weights(l) for l in p[:, 11]])               # (B, N)
    f = p[:, 10:11]
    h = TR.h_of_theta(s, torch.clamp(theta0, s["tr"] + 1e-4, s["ts"] - 1e-4))
    out = [TR.theta(s, h)]
    drain = torch.zeros(h.shape[0])
    total_miss = torch.zeros(h.shape[0])
    dt = 0.25
    for t in range(1, HOURS):
        done = 0.0
        while done < 1.0 - 1e-9:
            dt = min(dt, 1.0 - done)
            stress = 1 / (1 + (h / h50) ** 3)
            sink = f * et[:, t:t + 1] * w * stress
            h_new = TR.step(s, h, None, sink, dt, iters=iters, top_flux=water_in[:, t])
            q_out = TR.conductivity(s, h_new)[:, -1]
            miss = storage(s, h_new) - storage(s, h) - (water_in[:, t] - q_out - (sink * DZW).sum(-1)) * dt
            if miss.abs().max() > tol_cm and dt > 1 / 512:
                dt /= 2          # water balance off: redo with a smaller step
                continue
            h, done = h_new, done + dt
            drain += q_out * dt
            total_miss += miss.abs()
            dt = min(dt * 1.5, 0.25)  # it worked: try a bigger step next time
        out.append(TR.theta(s, h))
    th = torch.stack(out, dim=1)
    return (th, total_miss, drain) if return_drain else (th, total_miss)


def make_chunk(args):
    """Make `n` samples with their own random seed (one worker process does this)."""
    seed, n, batch = args
    torch.set_num_threads(1)              # each worker uses one CPU core
    g = torch.Generator().manual_seed(seed)
    et_days = load_et_days()
    keep = {k: [] for k in ("water_in", "et", "params", "theta", "region")}
    dropped = 0
    for i in range(0, n, batch):
        B = min(batch, n - i)
        regions = random_regions(B, g)
        s, p = random_soils(regions, g)
        et = random_et(regions, g, et_days)
        wi = random_water_in(regions, g, et)
        th, miss = simulate(s, p, wi, et, random_start(s, B, g))
        # keep only samples where the physics kept its water balance (miss < 0.05 cm = 0.5 mm per week)
        ok = torch.isfinite(th).all(dim=(1, 2)) & (miss < 0.05)
        dropped += int((~ok).sum())
        for k, v in (("water_in", wi), ("et", et), ("params", p), ("theta", th), ("region", regions)):
            keep[k].append(v[ok].float() if k != "region" else v[ok])
    return {k: torch.cat(v) for k, v in keep.items()}, dropped


if __name__ == "__main__":
    import os
    from multiprocessing import Pool
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--chunk", type=int, default=40, help="samples per job")
    ap.add_argument("--batch", type=int, default=8, help="samples solved together inside a job")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="synth_train.pt")
    a = ap.parse_args()

    jobs = [(a.seed * 100000 + j, min(a.chunk, a.n - j * a.chunk), a.batch) for j in range((a.n + a.chunk - 1) // a.chunk)]
    print(f"{a.n} samples in {len(jobs)} jobs on {a.workers} CPU cores")
    parts, dropped, t0 = [], 0, time.time()
    with Pool(a.workers) as pool:
        for k, (part, d) in enumerate(pool.imap_unordered(make_chunk, jobs), 1):
            parts.append(part)
            dropped += d
            done = sum(len(p["params"]) for p in parts) + dropped
            print(f"{done}/{a.n} samples ({dropped} dropped as broken), {time.time() - t0:.0f} s", flush=True)
    data = {k: torch.cat([p[k] for p in parts]) for k in parts[0]}
    data["z_cm"] = TR.Z.float()
    torch.save(data, a.out)
    counts = torch.bincount(data["region"], minlength=3).tolist()
    print("saved", a.out, {k: tuple(v.shape) for k, v in data.items()}, "sandy/loamy/wide:", counts)
