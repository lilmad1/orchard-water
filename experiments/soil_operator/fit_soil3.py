"""Soil test 4: NO PROBES. How well can we tell "plants used it" from "it drained away"
with only what a buyer's field gives us?

What we know on a field with no probe:
  - SOIL  from a soil map (US: SSURGO). A map tells you the soil CLASS (e.g. "sandy loam"),
          so the best it can give is that class's average numbers (Rosetta). The real field
          differs from the class average, like real soils do.
          We also try a map that names the WRONG class 30 % of the time.
  - ET    from an external ET product with an error, like fit_soil2.py:
          calibrated = random +-12 % per field, out of the box = +34 %.
  - WATER IN from the meter / delivery records (optionally +-5 % meter error).
  - ROOTS: we don't know the root depth -> one fixed guess (35 cm, middle of the range).
  - START: we don't know how wet the soil is -> start "30 % full" everywhere and let
          weeks 1-8 settle it (warm-up). Only weeks 9-16 are scored.

No fitting at all here: the FNO just runs the 16 weeks with these guesses.
The last row adds an ENSEMBLE: 16 runs with soils drawn around the map value. Its spread
is the honest "plus/minus" we would show a buyer; we check how often the truth falls inside.

Scores (weeks 9-16):
  profile error    m3/m3 at all 47 depths
  uptake / drainage miss   mm per week (average over fields and weeks)
  season drainage miss     |8-week total model - 8-week total truth| / truth, in %
                           (buyers count water over a season, so weekly errors can cancel)

Usage:
  python fit_soil3.py --data blocks16.pt --model fno_mix.pt
"""
import argparse
import torch
from fno import FNO, PARAM_LO, PARAM_HI, soil_limits
from fit_soil import chain, weekly_mm
from synth import SOIL_TYPES, SPREAD
torch.set_default_dtype(torch.float32)     # the solver files switch torch to float64; the FNO runs in float32

CLASSES = torch.tensor(list(SOIL_TYPES.values()))          # (7, 5) class averages
SPR = torch.tensor(SPREAD)                                  # how much real soils vary around the class
LAM_GUESS = 35.0
START_FULL = 0.3

# name, soil, ET error, start, roots, meter error
CONFIGS = [
    ("true soil, true ET, true start (best case)", "true", None, "true", "true", 0.0),
    ("no map (middle of range), ET +-12%", "middle", ("rand", 0.12), "guess", "guess", 0.0),
    ("map, ET +-12%", "map", ("rand", 0.12), "guess", "guess", 0.0),
    ("map 30% wrong class, ET +-12%", "map_wrong", ("rand", 0.12), "guess", "guess", 0.0),
    ("map, ET out of box +34%", "map", ("bias", 0.34), "guess", "guess", 0.0),
    ("map, ET +-12%, meter +-5%", "map", ("rand", 0.12), "guess", "guess", 0.05),
    ("map, ET +-12%, TRUE start", "map", ("rand", 0.12), "true", "guess", 0.0),
    ("map, ET +-12%, TRUE roots", "map", ("rand", 0.12), "guess", "true", 0.0),
    ("map, TRUE ET, true roots", "map", None, "guess", "true", 0.0),
]


def map_soil(p, g, wrong=0.0):
    """What a soil map gives: for each layer, the average numbers of the soil's class.
    The class = nearest class average (distance in units of normal soil variation).
    With `wrong` > 0 the map names a random other class that often."""
    out = p.clone()
    for k in range(2):
        layer = p[:, 5 * k:5 * k + 5]
        dist = (((layer[:, None, :] - CLASSES[None]) / SPR) ** 2).sum(-1)    # (B, 7)
        cls = dist.argmin(1)
        if wrong > 0:
            flip = torch.rand(len(p), generator=g) < wrong
            other = (cls + torch.randint(1, len(CLASSES), (len(p),), generator=g)) % len(CLASSES)
            cls = torch.where(flip, other, cls)
        out[:, 5 * k:5 * k + 5] = CLASSES[cls]
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="blocks16.pt")
    ap.add_argument("--model", default="fno_mix.pt")
    ap.add_argument("--n_blocks", type=int, default=64)
    ap.add_argument("--members", type=int, default=16, help="ensemble size for the plus/minus row")
    ap.add_argument("--bs", type=int, default=32)
    a = ap.parse_args()

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    d = torch.load(a.data)
    nb = min(a.n_blocks, len(d["params"]))
    p_cpu = d["params"][:nb].float()
    wi, et, p, th = (d[k][:nb].float().to(dev) for k in ("water_in", "et", "params", "theta"))
    W = th.shape[1]
    F0 = W // 2
    model = FNO().to(dev)
    model.load_state_dict(torch.load(a.model, map_location=dev))
    model.eval()
    lo, hi = PARAM_LO.to(dev), PARAM_HI.to(dev)
    Z = th.shape[-1]
    g = torch.Generator().manual_seed(0)
    rand_et = torch.randn(nb, generator=g).to(dev)           # same per-field errors in every row
    rand_meter = torch.randn(nb, generator=g).to(dev)
    maps = {"map": map_soil(p_cpu, g).to(dev), "map_wrong": map_soil(p_cpu, g, wrong=0.3).to(dev)}

    def build(soil, et_mode, start, roots, meter):
        if soil == "true":
            pp = p.clone()
        elif soil == "middle":
            pp = (lo + (hi - lo) * 0.5).expand(nb, 12).clone()
        else:
            pp = maps[soil].clone()
        pp[:, 10] = p[:, 10]                                  # ET from outside: the share is folded into ET
        pp[:, 11] = p[:, 11] if roots == "true" else LAM_GUESS
        et_m = et
        if et_mode is not None:
            kind, size = et_mode
            err = size * rand_et if kind == "rand" else torch.full((nb,), size, device=dev)
            et_m = et * (1 + err)[:, None, None]
        wi_m = wi * (1 + meter * rand_meter)[:, None, None]
        if start == "true":
            th0 = th[:, 0, 0]
        else:
            tr, ts = soil_limits(pp, Z)
            th0 = tr + START_FULL * (ts - tr)
        return pp, et_m, wi_m, th0

    @torch.no_grad()
    def run(pp, et_m, wi_m, th0):
        sim = torch.cat([chain(model, wi_m[j:j + a.bs], et_m[j:j + a.bs], pp[j:j + a.bs], th0[j:j + a.bs], W)
                         for j in range(0, len(pp), a.bs)])
        r, dr = weekly_mm(sim[:, F0:], pp, et_m[:, F0:])
        return sim, r, dr

    true_r, true_d = weekly_mm(th[:, F0:], p, et[:, F0:])
    true_season = true_d.sum(1)
    print(f"device {dev}: {nb} fields x {W} weeks, NO probes; weeks 1-{F0} = warm-up, weeks {F0 + 1}-{W} scored")
    print(f"truth in scored weeks: root uptake {true_r.mean().item():.1f} mm/week, drainage {true_d.mean().item():.1f} mm/week\n")

    rows = []
    for name, *cfg in CONFIGS:
        pp, et_m, wi_m, th0 = build(*cfg)
        sim, r, dr = run(pp, et_m, wi_m, th0)
        prof = ((sim[:, F0:] - th[:, F0:]) ** 2).mean().sqrt().item()
        season = (dr.sum(1) - true_season).abs().mean() / true_season.mean() * 100
        rows.append((name, prof, (r - true_r).abs().mean().item(), (dr - true_d).abs().mean().item(), season.item()))
        print(f"   ran {name}", flush=True)

    # ensemble: soils drawn around the (right-class) map value, ET +-12 %, start guessed, roots guessed
    M = a.members
    pp0, et_m, wi_m, _ = build("map", ("rand", 0.12), "guess", "guess", 0.0)
    noise = torch.randn(M, nb, 12, generator=g).to(dev)
    spr12 = torch.zeros(12, device=dev)
    spr12[:5] = SPR.to(dev)
    spr12[5:10] = SPR.to(dev)
    spr12[11] = 12.0                                         # root depth unknown too: +-12 cm
    ens = (pp0[None] + noise * spr12).clamp(lo, hi)        # (M, nb, 12)
    seasons, sims, rs, drs = [], [], [], []
    for m in range(M):
        tr, ts = soil_limits(ens[m], Z)
        sim, r, dr = run(ens[m], et_m, wi_m, tr + START_FULL * (ts - tr))
        sims.append(sim[:, F0:])
        rs.append(r)
        drs.append(dr)
        seasons.append(dr.sum(1))
    seasons = torch.stack(seasons)                           # (M, nb) 8-week drainage per member
    mean_sim = torch.stack(sims).mean(0)
    r_m, dr_m = torch.stack(rs).mean(0), torch.stack(drs).mean(0)
    s_m = seasons.mean(0)
    prof = ((mean_sim - th[:, F0:]) ** 2).mean().sqrt().item()
    rows.append((f"map + {M}-run ensemble (average), ET +-12%", prof, (r_m - true_r).abs().mean().item(),
                 (dr_m - true_d).abs().mean().item(), ((s_m - true_season).abs().mean() / true_season.mean() * 100).item()))
    q10, q90 = seasons.quantile(0.1, 0), seasons.quantile(0.9, 0)
    inside = ((true_season >= q10) & (true_season <= q90)).float().mean().item() * 100
    width = ((q90 - q10) / 2).mean().item() / true_season.mean().item() * 100

    print(f"\nFORECAST weeks {F0 + 1}-{W}, {nb} fields, NO probes")
    print(f"   {'setup':46s}{'profile':>9s}{'uptake miss':>13s}{'drain miss':>12s}{'season drain':>14s}")
    print(f"   {'':46s}{'(m3/m3)':>9s}{'(mm/wk)':>13s}{'(mm/wk)':>12s}{'miss (%)':>14s}")
    for name, pr, r, dr, s in rows:
        print(f"   {name:46s}{pr:9.4f}{r:13.2f}{dr:12.2f}{s:14.0f}")
    print(f"\n   ensemble plus/minus band (10-90 %): about +-{width:.0f} % of season drainage;"
          f" truth inside the band on {inside:.0f} % of fields (should be about 80 %)")
