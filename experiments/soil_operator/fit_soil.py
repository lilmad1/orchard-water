"""Soil recovery test 2: several weeks of the same block, and different probe layouts.

For each test block (from synth_blocks.py) we hide the 12 soil numbers and show only
what the probes would measure. The FNO runs the weeks one after another (each week
starts where its own last week ended) and we let it find the soil numbers that best
match the probes (gradient through the FNO, like check_fno.py).

Then the real question: with that soil, FORECAST the last 4 weeks (never shown)
and compare at ALL 47 depths, plus the two numbers a "water saved" report needs:
  - root uptake: water the trees really used (mm per week)
  - drainage:    water lost below the roots (mm per week)

We try:
  - fitting on 1 week vs 4 weeks of probe data
  - 3 probe depths (20/40/60 cm, like probe v1.3), 4 depths (20/30/60/80), 6 depths (10/20/40/60/80/100)

Honest limits: the start profile of week 1 is given at all depths (in real life we
only know it at the probes), and the physics is perfect (real farms are messier).

Usage (GPU, same window):
  python fit_soil.py --data blocks_test.pt --model fno_20k.pt
"""
import argparse, time
import torch
from fno import FNO, PARAM_LO, PARAM_HI, soil_limits
from train_fno import layer_values, DZ, H50

LAYOUTS = {"3 depths 20/40/60": [20, 40, 60],
           "4 depths 20/30/60/80": [20, 30, 60, 80],
           "6 depths 10..100": [10, 20, 40, 60, 80, 100]}


def fluxes(theta, params, et):
    """Root uptake and bottom drainage, each (B, T) in cm/h, from water content (same formulas as train_fno.py)."""
    B, T, Z = theta.shape
    tr, ts = soil_limits(params, Z)
    a = 10 ** layer_values(params, Z, 2)
    n = layer_values(params, Z, 3)
    ks = 10 ** layer_values(params, Z, 4)
    m = 1 - 1 / n
    se = ((theta - tr[:, None]) / (ts - tr)[:, None]).clamp(1e-6, 0.999)
    h = -((se ** (-1 / m[:, None]) - 1) ** (1 / n[:, None])) / a[:, None]
    sb, mb = se[..., -1], m[:, None, -1]
    drain = ks[:, None, -1] * sb.sqrt() * (1 - (1 - sb ** (1 / mb)) ** mb) ** 2
    z = 5 + DZ * torch.arange(Z, device=theta.device)
    w = torch.where((z > 5) & (z <= 100), torch.exp(-z / params[:, 11:12]), torch.zeros((), device=theta.device))
    w = w / (w.sum(-1, keepdim=True) * DZ)
    dzw = torch.full((Z,), DZ, device=theta.device)
    dzw[0] = dzw[-1] = DZ / 2
    stress = 1 / (1 + (h / H50) ** 3)
    roots = (params[:, 10, None, None] * et[:, :, None] * w[:, None, :] * stress * dzw).sum(-1)
    return roots, drain


def chain(model, wi, et, p, th0, weeks):
    """Run the FNO week after week; each week starts where the previous one ended. -> (B, weeks, 168, 47)"""
    out = []
    for k in range(weeks):
        th = model(wi[:, k], et[:, k], p, th0)
        out.append(th)
        th0 = th[:, -1]
    return torch.stack(out, 1)


def weekly_mm(theta_w, p, et_w):
    """(B, W, 168, 47) -> weekly root uptake and drainage in mm, each (B, W)."""
    B, W = theta_w.shape[:2]
    r, d = fluxes(theta_w.flatten(0, 1), p.repeat_interleave(W, 0), et_w.flatten(0, 1))
    return r.sum(1).view(B, W) * 10, d.sum(1).view(B, W) * 10


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="blocks_test.pt")
    ap.add_argument("--model", default="fno_20k.pt")
    ap.add_argument("--n_blocks", type=int, default=64)
    ap.add_argument("--bs", type=int, default=16, help="blocks fitted together (lower it if the GPU runs out of memory)")
    ap.add_argument("--steps", type=int, default=300)
    ap.add_argument("--fit_weeks", type=int, nargs="+", default=[1, 4])
    a = ap.parse_args()

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    d = torch.load(a.data)
    nb = min(a.n_blocks, len(d["params"]))
    wi, et, p, th = (d[k][:nb].to(dev) for k in ("water_in", "et", "params", "theta"))
    W = th.shape[1]
    F0 = W // 2                                   # forecast = second half of the weeks
    assert max(a.fit_weeks) <= F0, "fit weeks must be in the first half"
    model = FNO().to(dev)
    model.load_state_dict(torch.load(a.model, map_location=dev))
    model.eval()
    for q in model.parameters():
        q.requires_grad_(False)
    lo, hi = PARAM_LO.to(dev), PARAM_HI.to(dev)
    print(f"device {dev}: {nb} blocks x {W} weeks; fit on the first weeks, forecast weeks {F0 + 1}-{W}\n")

    # the truth, and two reference soils
    true_r, true_d = weekly_mm(th[:, F0:], p, et[:, F0:])
    def score(pp):
        with torch.no_grad():
            sim = torch.cat([chain(model, wi[j:j + a.bs], et[j:j + a.bs], pp[j:j + a.bs], th[j:j + a.bs, 0, 0], W)
                             for j in range(0, nb, a.bs)])
        r, dr = weekly_mm(sim[:, F0:], pp, et[:, F0:])
        prof = ((sim[:, F0:] - th[:, F0:]) ** 2).mean().sqrt().item()
        return prof, (r - true_r).abs().mean().item(), (dr - true_d).abs().mean().item()

    print(f"truth in forecast weeks: root uptake {true_r.mean().item():.1f} mm/week, drainage {true_d.mean().item():.1f} mm/week (averages)\n")
    rows = [("true soil (best case)", *score(p)),
            ("middle guess, no fitting", *score((lo + (hi - lo) * 0.5).expand(nb, 12)))]

    for name, depths in LAYOUTS.items():
        idx = [int(round((c - 5) / 2.5)) for c in depths]
        for K in a.fit_weeks:
            t0 = time.time()
            found = []
            for j in range(0, nb, a.bs):
                sl = slice(j, j + a.bs)
                x = torch.zeros(len(p[sl]), 12, device=dev, requires_grad=True)
                opt = torch.optim.Adam([x], lr=0.05)
                obs = th[sl, :K][..., idx]
                for step in range(a.steps):
                    pf = lo + (hi - lo) * torch.sigmoid(x)
                    sim = chain(model, wi[sl], et[sl], pf, th[sl, 0, 0], K)
                    loss = ((sim[..., idx] - obs) ** 2).mean()
                    opt.zero_grad()
                    loss.backward()
                    opt.step()
                found.append((lo + (hi - lo) * torch.sigmoid(x)).detach())
            found = torch.cat(found)
            rows.append((f"{name}, {K} week{'s' if K > 1 else ''}", *score(found)))
            print(f"   fitted {name}, {K} week(s): probe error {loss.sqrt().item():.4f}  ({time.time() - t0:.0f} s)", flush=True)

    print(f"\nFORECAST weeks {F0 + 1}-{W} (never shown), {nb} blocks")
    print(f"   {'soil':38s}{'profile error':>14s}{'root uptake miss':>18s}{'drainage miss':>15s}")
    print(f"   {'':38s}{'(m3/m3)':>14s}{'(mm/week)':>18s}{'(mm/week)':>15s}")
    for name, prof, r, dr in rows:
        print(f"   {name:38s}{prof:14.4f}{r:18.2f}{dr:15.2f}")
