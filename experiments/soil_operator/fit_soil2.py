"""Soil recovery test 3: can we split "plants used it" from "it drained away"?

Same idea as fit_soil.py (hide the soil, show only probes, let the FNO find the soil,
then forecast weeks it never saw), but now on 16-week blocks and with 3 new tricks:

  a. ET FROM OUTSIDE: the plants' water use comes from our FAO engine instead of being
                guessed from the probes. The engine is not perfect, so we feed it with the
                assumed ET errors (see CONFIGS).
                The same wrong ET is used in the forecast weeks too, like in real life.
  b. DEEP POINT: one extra probe reading at 120 cm, where drainage leaves the column.
  c. MORE WEEKS: fit on 8 weeks instead of 4.

Forecast = weeks 9-16 for every row, so all rows are compared on the same weeks.
Same honest limits as before: start profile known, perfect physics.

Usage:
  python synth_blocks.py --blocks 140 --weeks 16 --seed 23 --out blocks16.pt
  python fit_soil2.py --data blocks16.pt --model fno_mix.pt
"""
import argparse, time
import torch
from fno import FNO, PARAM_LO, PARAM_HI
from fit_soil import chain, weekly_mm

SIX = [10, 20, 40, 60, 80, 100]
# ET from outside = an FAO-style ET engine. How wrong it can be is an ASSUMPTION here:
#   "out of the box": a +34 % or +13 % bias over the season (assumed)
#   "calibrated per farm": no bias, but a random +-12 % per block (assumed)
CONFIGS = [  # name, probe depths (cm), fit weeks, ET from outside: None = learn it, ("rand", s) or ("bias", b)
    ("6 depths, 4 wk, ET learned", SIX, 4, None),
    ("6 depths, 4 wk, ET calibrated (+-12%)", SIX, 4, ("rand", 0.12)),
    ("6 depths, 4 wk, ET out of box (+34%)", SIX, 4, ("bias", 0.34)),
    ("6 depths, 4 wk, ET out of box (+13%)", SIX, 4, ("bias", 0.13)),
    ("6 depths, 8 wk, ET learned", SIX, 8, None),
    ("6 depths + 120 cm, 4 wk, ET learned", SIX + [120], 4, None),
    ("6+120 cm, 8 wk, ET calibrated", SIX + [120], 8, ("rand", 0.12)),
    ("3 depths 20/40/60, 8 wk, ET calibrated", [20, 40, 60], 8, ("rand", 0.12)),
]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="blocks16.pt")
    ap.add_argument("--model", default="fno_mix.pt")
    ap.add_argument("--n_blocks", type=int, default=64)
    ap.add_argument("--steps", type=int, default=300)
    a = ap.parse_args()

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    d = torch.load(a.data)
    nb = min(a.n_blocks, len(d["params"]))
    wi, et, p, th = (d[k][:nb].to(dev) for k in ("water_in", "et", "params", "theta"))
    W = th.shape[1]
    F0 = W // 2
    model = FNO().to(dev)
    model.load_state_dict(torch.load(a.model, map_location=dev))
    model.eval()
    for q in model.parameters():
        q.requires_grad_(False)
    lo, hi = PARAM_LO.to(dev), PARAM_HI.to(dev)
    g = torch.Generator().manual_seed(0)
    rand_err = torch.randn(nb, generator=g).to(dev)          # one fixed error per block, same for every config

    def et_for(mode):
        """The ET the model believes: true ET, or the engine's ET with its error."""
        if mode is None:
            return et
        kind, size = mode
        err = size * rand_err if kind == "rand" else torch.full((nb,), size, device=dev)
        return et * (1 + err)[:, None, None]
    print(f"device {dev}: {nb} blocks x {W} weeks; forecast weeks {F0 + 1}-{W}\n")

    true_r, true_d = weekly_mm(th[:, F0:], p, et[:, F0:])

    def score(pp, et_m, bs=8):
        with torch.no_grad():
            sim = torch.cat([chain(model, wi[j:j + bs], et_m[j:j + bs], pp[j:j + bs], th[j:j + bs, 0, 0], W)
                             for j in range(0, nb, bs)])
        r, dr = weekly_mm(sim[:, F0:], pp, et_m[:, F0:])          # what the MODEL thinks happened
        return (((sim[:, F0:] - th[:, F0:]) ** 2).mean().sqrt().item(),
                (r - true_r).abs().mean().item(), (dr - true_d).abs().mean().item())

    print(f"truth in forecast weeks: root uptake {true_r.mean().item():.1f} mm/week, drainage {true_d.mean().item():.1f} mm/week\n")
    rows = [("true soil, true ET (best case)", *score(p, et))]
    for name, depths, K, et_mode in CONFIGS:
        et_m = et_for(et_mode)
        idx = [int(round((c - 5) / 2.5)) for c in depths]
        bs = max(4, 64 // K)                      # fewer blocks at once when the chain is long (GPU memory)
        t0, found = time.time(), []
        for j in range(0, nb, bs):
            sl = slice(j, j + bs)
            x = torch.zeros(len(p[sl]), 12, device=dev, requires_grad=True)
            opt = torch.optim.Adam([x], lr=0.05)
            obs = th[sl, :K][..., idx]
            for step in range(a.steps):
                pf = lo + (hi - lo) * torch.sigmoid(x)
                if et_mode is not None:           # plant use comes from the engine: keep f fixed, ET carries the error
                    pf = torch.cat([pf[:, :10], p[sl, 10:11], pf[:, 11:]], 1)
                sim = chain(model, wi[sl], et_m[sl], pf, th[sl, 0, 0], K)
                loss = ((sim[..., idx] - obs) ** 2).mean()
                opt.zero_grad()
                loss.backward()
                opt.step()
            found.append(pf.detach())
        found = torch.cat(found)
        rows.append((name, *score(found, et_m)))
        print(f"   fitted {name}: probe error {loss.sqrt().item():.4f}  ({time.time() - t0:.0f} s)", flush=True)

    print(f"\nFORECAST weeks {F0 + 1}-{W} (never shown), {nb} blocks")
    print(f"   {'soil':42s}{'profile error':>14s}{'root uptake miss':>18s}{'drainage miss':>15s}")
    print(f"   {'':42s}{'(m3/m3)':>14s}{'(mm/week)':>18s}{'(mm/week)':>15s}")
    for name, prof, r, dr in rows:
        print(f"   {name:42s}{prof:14.4f}{r:18.2f}{dr:15.2f}")
