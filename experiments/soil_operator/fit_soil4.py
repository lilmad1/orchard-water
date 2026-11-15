"""Soil test 5: no probes, part 2. Three things on top of fit_soil3.py:

  1. HONEST PLUS/MINUS. The ensemble now also varies what we don't know besides the soil:
     ET (+-12 %), meter (+-5 %), root depth, start wetness, and (for a class map) the chance
     the map named the wrong class. We check how often the truth falls inside the 10-90 %
     band (should be ~80 %). `spread x2` = the soil part of the band made twice as wide.
  2. BETTER MAP. A class map gives the class average. A texture map (US: SSURGO sand/silt/clay %)
     lands closer: here we GUESS it halves the soil error (to check against real SSURGO later).
  3. SATELLITE TOP SOIL. A daily reading of the top ~5-10 cm from radar satellites, used as a
     weak "fake probe": noise +-0.04 m3/m3 every day (+-0.02 for a good product) and a fixed
     bias per field (+-0.03). We fit the soil, the ET error and that bias to it, pulled
     towards the map (the map = what we believe before seeing any reading).
     Water-saved numbers are counted AFTER the season, so the fit may use all 16 weeks
     of readings. Drainage itself is never shown to it.

All rows: ET +-12 %, meter +-5 %, start guessed (30 % full) with weeks 1-8 as warm-up,
root depth guessed (35 cm). Weeks 9-16 are scored, like fit_soil3.py.
Honest limit: 1D column. A real satellite pixel (100 m+) mixes wet drip spots and dry soil
between rows; that can't be tested here and may make the satellite much weaker in orchards.

Usage:
  python fit_soil4.py --data blocks16.pt --model fno_mix.pt
"""
import argparse, time
import torch
from fno import FNO, PARAM_LO, PARAM_HI, soil_limits
from fit_soil import chain, weekly_mm
from fit_soil3 import map_soil, CLASSES, SPR, LAM_GUESS, START_FULL

ET_ERR, METER_ERR, SAT_BIAS, LAM_SD, WRONG = 0.12, 0.05, 0.03, 12.0, 0.3

# name, map, satellite noise (None = no satellite)
CONFIGS = [
    ("true soil (only ET/meter/start errors)", "true", None),
    ("class map", "map", None),
    ("class map, 30% wrong class", "map_wrong", None),
    ("texture map", "texture", None),
    ("class map + satellite", "map", 0.04),
    ("class map 30% wrong + satellite", "map_wrong", 0.04),
    ("texture map + satellite", "texture", 0.04),
    ("texture map + good satellite (+-0.02)", "texture", 0.02),
]
PRIOR_SHARE = {"true": 1.0, "map": 1.0, "map_wrong": 1.0, "texture": 0.5}   # soil spread around each map


def texture_map(p, g, share=0.5):
    """A map that knows the sand/clay %: lands `share` x the normal soil variation away from the truth."""
    out = p.clone()
    for k in range(2):
        out[:, 5 * k:5 * k + 5] += share * SPR * torch.randn(len(p), 5, generator=g)
    eps = 1e-3 * (PARAM_HI - PARAM_LO)
    return torch.max(torch.min(out, PARAM_HI - eps), PARAM_LO + eps)


def daily_top(theta):
    """(B, K, 168, 47) -> (B, K*7): daily average water content of the top 5-10 cm (what a satellite sees)."""
    return theta[..., :3].mean(-1).reshape(theta.shape[0], -1, 24).mean(-1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="blocks16.pt")
    ap.add_argument("--model", default="fno_mix.pt")
    ap.add_argument("--n_blocks", type=int, default=64)
    ap.add_argument("--steps", type=int, default=200)
    ap.add_argument("--members", type=int, default=32)
    ap.add_argument("--bs", type=int, default=32)
    a = ap.parse_args()

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    d = torch.load(a.data)
    nb = min(a.n_blocks, len(d["params"]))
    p_cpu = d["params"][:nb].float()
    wi, et, p, th = (d[k][:nb].float().to(dev) for k in ("water_in", "et", "params", "theta"))
    W = th.shape[1]
    F0, Z = W // 2, th.shape[-1]
    model = FNO().to(dev)
    model.load_state_dict(torch.load(a.model, map_location=dev))
    model.eval()
    for q in model.parameters():
        q.requires_grad_(False)
    lo, hi = PARAM_LO.to(dev), PARAM_HI.to(dev)

    g = torch.Generator().manual_seed(0)                       # same order as fit_soil3 -> same fields, same maps
    rand_et = torch.randn(nb, generator=g).to(dev)
    rand_meter = torch.randn(nb, generator=g).to(dev)
    maps = {"map": map_soil(p_cpu, g).to(dev), "map_wrong": map_soil(p_cpu, g, wrong=WRONG).to(dev)}
    maps["texture"] = texture_map(p_cpu, g).to(dev)
    maps["true"] = p.clone()
    for m in maps.values():
        m[:, 10] = p[:, 10]                                    # ET from outside: the share is folded into ET
        m[:, 11] = p[:, 11] if m is maps["true"] else LAM_GUESS
    et_b = et * (1 + ET_ERR * rand_et)[:, None, None]          # the ET we believe
    wi_b = wi * (1 + METER_ERR * rand_meter)[:, None, None]    # the water in the meter says
    sat_noise = torch.randn(nb, W * 7, generator=g).to(dev)
    sat_bias = SAT_BIAS * torch.randn(nb, generator=g).to(dev)
    sat_clean = daily_top(th)

    def start_guess(pp, full=START_FULL):
        tr, ts = soil_limits(pp, Z)
        full = full if torch.is_tensor(full) else torch.full((len(pp),), full, device=dev)
        return tr + full[:, None] * (ts - tr)

    @torch.no_grad()
    def forecast(pp, et_m, wi_m, th0):
        sim = torch.cat([chain(model, wi_m[j:j + a.bs], et_m[j:j + a.bs], pp[j:j + a.bs], th0[j:j + a.bs], W)
                         for j in range(0, len(pp), a.bs)])
        r, dr = weekly_mm(sim[:, F0:], pp, et_m[:, F0:])
        return sim, r, dr

    true_r, true_d = weekly_mm(th[:, F0:], p, et[:, F0:])
    true_season = true_d.sum(1)

    def score(sim, r, dr):
        return (((sim[:, F0:] - th[:, F0:]) ** 2).mean().sqrt().item(), (r - true_r).abs().mean().item(),
                (dr - true_d).abs().mean().item(),
                ((dr.sum(1) - true_season).abs().mean() / true_season.mean() * 100).item())

    def fit_satellite(prior, share, sd):
        """Find soil + ET error + satellite bias that match the daily top-soil readings, pulled to the map."""
        obs = sat_clean + sd * sat_noise + sat_bias[:, None]
        scale = torch.cat([SPR, SPR]).to(dev) * share
        bs = max(2, 64 // W)
        out_p, out_e = [], []
        for j in range(0, nb, bs):
            sl = slice(j, j + bs)
            m = prior[sl]
            u = ((m - lo) / (hi - lo)).clamp(0.01, 0.99)
            x = torch.log(u / (1 - u)).clone().requires_grad_(True)
            e = torch.zeros(len(m), device=dev, requires_grad=True)
            b = torch.zeros(len(m), device=dev, requires_grad=True)
            opt = torch.optim.Adam([x, e, b], lr=0.05)
            for step in range(a.steps):
                pf = lo + (hi - lo) * torch.sigmoid(x)
                pf = torch.cat([pf[:, :10], p[sl, 10:11], pf[:, 11:]], 1)
                et_f = et_b[sl] * (1 + e)[:, None, None]
                sim = chain(model, wi_b[sl], et_f, pf, start_guess(pf), W)
                miss = ((daily_top(sim) + b[:, None] - obs[sl]) / sd) ** 2
                prior_pen = (((pf[:, :10] - m[:, :10]) / scale) ** 2).sum(1) + ((pf[:, 11] - LAM_GUESS) / LAM_SD) ** 2 \
                    + (e / ET_ERR) ** 2 + (b / SAT_BIAS) ** 2
                loss = (miss.sum(1) + prior_pen).mean()
                opt.zero_grad()
                loss.backward()
                opt.step()
            out_p.append(pf.detach())
            out_e.append(e.detach())
        return torch.cat(out_p), torch.cat(out_e)

    print(f"device {dev}: {nb} fields x {W} weeks, NO probes, ET +-12%, meter +-5%; weeks {F0 + 1}-{W} scored")
    print(f"truth in scored weeks: root uptake {true_r.mean().item():.1f} mm/week, drainage {true_d.mean().item():.1f} mm/week\n")
    rows = []
    for name, mk, sd in CONFIGS:
        t0 = time.time()
        if sd is None:
            pp, et_m = maps[mk], et_b
        else:
            pp, e = fit_satellite(maps[mk], PRIOR_SHARE[mk], sd)
            et_m = et_b * (1 + e)[:, None, None]
        rows.append((name, *score(*forecast(pp, et_m, wi_b, start_guess(pp)))))
        print(f"   ran {name} ({time.time() - t0:.0f} s)", flush=True)

    # ---------- honest plus/minus ----------
    def ensemble(mk, k, M):
        gg = torch.Generator().manual_seed(1)
        base = maps[mk]
        seasons = []
        for _ in range(M):
            pp = base.clone()
            for L in range(2):
                sl = slice(5 * L, 5 * L + 5)
                if mk == "map_wrong":                          # we don't know which fields the map got wrong
                    swap = (torch.rand(nb, generator=gg) < WRONG).to(dev)
                    other = CLASSES[torch.randint(0, len(CLASSES), (nb,), generator=gg)].to(dev)
                    pp[:, sl] = torch.where(swap[:, None], other, pp[:, sl])
                pp[:, sl] += k * PRIOR_SHARE[mk] * SPR.to(dev) * torch.randn(nb, 5, generator=gg).to(dev)
            pp[:, 11] = LAM_GUESS + LAM_SD * torch.randn(nb, generator=gg).to(dev)
            pp = torch.max(torch.min(pp, hi), lo)
            et_m = et_b * (1 + ET_ERR * torch.randn(nb, generator=gg).to(dev))[:, None, None]
            wi_m = wi_b * (1 + METER_ERR * torch.randn(nb, generator=gg).to(dev))[:, None, None]
            full = (0.05 + 0.5 * torch.rand(nb, generator=gg)).to(dev)
            _, _, dr = forecast(pp, et_m, wi_m, start_guess(pp, full))
            seasons.append(dr.sum(1))
        s = torch.stack(seasons)
        q10, q50, q90 = s.quantile(0.1, 0), s.quantile(0.5, 0), s.quantile(0.9, 0)
        inside = ((true_season >= q10) & (true_season <= q90)).float().mean().item() * 100
        width = ((q90 - q10) / 2).mean().item() / true_season.mean().item() * 100
        miss = ((q50 - true_season).abs().mean() / true_season.mean() * 100).item()
        return width, inside, miss

    ens_rows = []
    for mk, label in (("map_wrong", "class map (30% wrong)"), ("texture", "texture map")):
        for k in (1, 2):
            ens_rows.append((f"{label}, soil spread x{k}", *ensemble(mk, k, a.members)))
        print(f"   ran ensembles for {label}", flush=True)

    print(f"\nFORECAST weeks {F0 + 1}-{W}, {nb} fields, NO probes")
    print(f"   {'setup':42s}{'profile':>9s}{'uptake miss':>13s}{'drain miss':>12s}{'season drain':>14s}")
    print(f"   {'':42s}{'(m3/m3)':>9s}{'(mm/wk)':>13s}{'(mm/wk)':>12s}{'miss (%)':>14s}")
    for name, pr, r, dr, s in rows:
        print(f"   {name:42s}{pr:9.4f}{r:13.2f}{dr:12.2f}{s:14.0f}")
    print(f"\nHONEST PLUS/MINUS ({a.members} runs per field, season drainage)")
    print(f"   {'ensemble':42s}{'band +-':>9s}{'truth inside':>14s}{'middle miss':>13s}")
    print(f"   {'':42s}{'(%)':>9s}{'(%, want ~80)':>14s}{'(%)':>13s}")
    for name, w, ins, ms in ens_rows:
        print(f"   {name:42s}{w:9.0f}{ins:14.0f}{ms:13.0f}")
