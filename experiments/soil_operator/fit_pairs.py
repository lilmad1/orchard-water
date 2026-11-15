"""BEFORE vs AFTER: how well do we get the SAVED water on a field with no probes?

Data from synth_pairs.py: same field, two seasons (before = usual watering, after = 60-90 %).
The model runs both seasons with what a real project has: soil map, satellite-style ET
with an error, meter with an error, guessed start and roots (like fit_soil3/4.py).

Three kinds of input error:
  steady    the same ET and meter error in both seasons (same satellite, same meter)
  changing  a new, independent error each season (cloudier year, trees pruned, new meter)
  mixed     most of the error steady, part of it changing (most realistic guess)

Scores (scored weeks of each season):
  one season     drainage miss for ONE season, in % (what fit_soil4.py measured)
  saved drain    |model (before - after) - truth (before - after)| / average true saving, in %
  saved use      the same for plant use
  drain share    the share of saved water that was "less drainage", model vs truth (points)

Usage:
  python fit_pairs.py --data pairs.pt --model fno_mix.pt
"""
import argparse
import torch
from fno import FNO, PARAM_LO, PARAM_HI, soil_limits
from fit_soil import chain, weekly_mm
from fit_soil3 import map_soil, LAM_GUESS, START_FULL

ET_ERR, METER_ERR = 0.12, 0.05
# name, soil, ET/meter error mode
CONFIGS = [
    ("true soil, no input errors (model limit)", "true", "none"),
    ("true soil, steady errors", "true", "steady"),
    ("class map, steady errors", "map", "steady"),
    ("class map, mixed errors", "map", "mixed"),
    ("class map, changing errors", "map", "changing"),
    ("map 30% wrong class, steady errors", "map_wrong", "steady"),
    ("map 30% wrong class, mixed errors", "map_wrong", "mixed"),
]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="pairs.pt")
    ap.add_argument("--model", default="fno_mix.pt")
    ap.add_argument("--bs", type=int, default=32)
    a = ap.parse_args()

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    d = torch.load(a.data)
    wi, et, p, th = (d[k].float().to(dev) for k in ("water_in", "et", "params", "theta"))   # wi/et (B,2,W,168)
    nb, W, Z = th.shape[0], th.shape[2], th.shape[-1]
    F0 = W // 2
    model = FNO().to(dev)
    model.load_state_dict(torch.load(a.model, map_location=dev))
    model.eval()
    g = torch.Generator().manual_seed(0)
    maps = {"true": p.clone(),
            "map": map_soil(p.cpu(), g).to(dev),
            "map_wrong": map_soil(p.cpu(), g, wrong=0.3).to(dev)}
    for k, m in maps.items():
        m[:, 10] = p[:, 10]                                   # ET from outside: the share is folded into ET
        m[:, 11] = p[:, 11] if k == "true" else LAM_GUESS
    z_steady = torch.randn(2, nb, generator=g).to(dev)          # [ET, meter] steady part
    z_season = torch.randn(2, 2, nb, generator=g).to(dev)       # [ET, meter] x season, changing part

    def errors(mode, season):
        """(ET error, meter error) per field for one season."""
        if mode == "none":
            return torch.zeros(nb, device=dev), torch.zeros(nb, device=dev)
        if mode == "steady":
            mix = z_steady
        elif mode == "changing":
            mix = z_season[:, season]
        else:                                                    # mixed: 80 % of the variance steady, 20 % changing
            mix = 0.894 * z_steady + 0.447 * z_season[:, season]
        return ET_ERR * mix[0], METER_ERR * mix[1]

    @torch.no_grad()
    def run(pp, et_m, wi_m, th0):
        sim = torch.cat([chain(model, wi_m[j:j + a.bs], et_m[j:j + a.bs], pp[j:j + a.bs], th0[j:j + a.bs], W)
                         for j in range(0, nb, a.bs)])
        r, dr = weekly_mm(sim[:, F0:], pp, et_m[:, F0:])
        return r.sum(1), dr.sum(1)                              # scored-weeks totals, mm

    true_u, true_d = [], []
    for s in range(2):
        r, dr = weekly_mm(th[:, s, F0:], p, et[:, s, F0:])
        true_u.append(r.sum(1))
        true_d.append(dr.sum(1))
    sav_d, sav_u = true_d[0] - true_d[1], true_u[0] - true_u[1]
    water_saved = (wi[:, 0, F0:].sum((1, 2)) - wi[:, 1, F0:].sum((1, 2))) * 10
    share_true = sav_d / (sav_d + sav_u)
    print(f"device {dev}: {nb} fields, 2 seasons x {W} weeks, weeks {F0 + 1}-{W} scored")
    print(f"truth per field over the scored weeks: water in cut by {water_saved.mean():.0f} mm;"
          f" drainage {true_d[0].mean():.0f} -> {true_d[1].mean():.0f} mm, plant use {true_u[0].mean():.0f} -> {true_u[1].mean():.0f} mm")
    print(f"   so on average {share_true.median() * 100:.0f} % of the saved water was 'less drainage' (median)\n")

    rows = []
    for name, soil, mode in CONFIGS:
        pp = maps[soil]
        tr, ts = soil_limits(pp, Z)
        th0 = tr + START_FULL * (ts - tr)                     # start unknown: guessed, warm-up weeks settle it
        u, dr = [], []
        for s in range(2):
            e_et, e_m = errors(mode, s)
            uu, dd = run(pp, et[:, s] * (1 + e_et)[:, None, None], wi[:, s] * (1 + e_m)[:, None, None], th0)
            u.append(uu)
            dr.append(dd)
        one = ((dr[0] - true_d[0]).abs().mean() / true_d[0].mean() * 100).item()
        ps_d, ps_u = dr[0] - dr[1], u[0] - u[1]
        miss_d = ((ps_d - sav_d).abs().mean() / sav_d.abs().mean() * 100).item()
        miss_u = ((ps_u - sav_u).abs().mean() / sav_u.abs().mean() * 100).item()
        share = (ps_d / (ps_d + ps_u) - share_true).abs().median().item() * 100
        rows.append((name, one, miss_d, miss_u, share))
        print(f"   ran {name}", flush=True)

    print(f"\nBEFORE vs AFTER, {nb} fields, NO probes, ET +-12 %, meter +-5 %")
    print(f"   {'setup':42s}{'one season':>11s}{'saved drain':>13s}{'saved use':>11s}{'drain share':>13s}")
    print(f"   {'':42s}{'miss (%)':>11s}{'miss (%)':>13s}{'miss (%)':>11s}{'miss (pts)':>13s}")
    for name, one, md, mu, sh in rows:
        print(f"   {name:42s}{one:11.0f}{md:13.0f}{mu:11.0f}{sh:13.0f}")
