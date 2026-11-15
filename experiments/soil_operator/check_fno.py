"""Check the trained FNO in 4 ways, all on the kept-aside weeks it never trained on.

  1. WHERE is it wrong?   error per region (sandy / loamy / wide) and per depth
  2. WATER over a week:   does it invent or lose water when you add up a whole week?
  3. SPEED:               how fast is it compared with our solver?
  4. SOIL RECOVERY:       the real test for the product. We hide a week's 12 soil numbers,
                          give the FNO only what 4 probes would see (20, 30, 60, 80 cm),
                          and let it find the soil numbers by itself (gradient through the FNO).
                          Then: does that soil predict ALL 47 depths well, including the
                          depths no probe saw? And are the soil numbers close to the truth?

Usage (same window as training):
  python check_fno.py --data synth_20k.pt --model fno_20k.pt
"""
import argparse, time
import torch
from fno import FNO, PARAM_LO, PARAM_HI
from train_fno import water_balance_miss

REGIONS = ["sandy", "loamy", "wide"]
PROBE_CM = [20, 30, 60, 80]
NAMES = [f"{n}_{k}" for k in ("top", "bot") for n in ("tr", "ts", "log10a", "n", "log10ks")] + ["f", "lam"]


def rmse(a, b, dim=None):
    return ((a - b) ** 2).mean(dim=dim).sqrt() if dim is not None else ((a - b) ** 2).mean().sqrt().item()


def predict(model, wi, et, p, th0, bs=64):
    with torch.no_grad():
        return torch.cat([model(wi[j:j + bs], et[j:j + bs], p[j:j + bs], th0[j:j + bs]) for j in range(0, len(p), bs)])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="synth_20k.pt")
    ap.add_argument("--model", default="fno_20k.pt")
    ap.add_argument("--n_fit", type=int, default=64, help="how many weeks to try soil recovery on")
    ap.add_argument("--steps", type=int, default=400, help="learning steps for soil recovery")
    a = ap.parse_args()

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    # the SAME kept-aside weeks as train_fno.py (same seed, same shuffle)
    torch.manual_seed(0)
    d = torch.load(a.data)
    n = len(d["params"])
    perm = torch.randperm(n)
    val = perm[:max(1, int(0.15 * n))]
    wi, et, p, th = (d[k][val].to(dev) for k in ("water_in", "et", "params", "theta"))
    region = d["region"][val].to(dev)
    z = d["z_cm"]
    model = FNO().to(dev)
    model.load_state_dict(torch.load(a.model, map_location=dev))
    model.eval()
    for q in model.parameters():
        q.requires_grad_(False)
    print(f"device {dev}, {len(val)} kept-aside weeks\n")

    # ---------- 1. where is it wrong ----------
    pred = predict(model, wi, et, p, th[:, 0])
    print("1. ERROR (m3/m3) on kept-aside weeks")
    print(f"   all weeks            {rmse(pred, th):.4f}")
    for r in range(3):
        m = region == r
        if m.any():
            print(f"   {REGIONS[r]:<8} {int(m.sum()):5d} weeks {rmse(pred[m], th[m]):.4f}")
    per_week = rmse(pred, th, dim=(1, 2))
    print(f"   worst 5% of weeks    {per_week.quantile(0.95).item():.4f} or worse   (single worst {per_week.max().item():.4f})")
    per_depth = rmse(pred, th, dim=(0, 1)).cpu()
    for lo, hi in ((5, 15), (15, 40), (40, 80), (80, 121)):
        m = (z >= lo) & (z < hi)
        print(f"   depth {lo:3d}-{hi - 1:3d} cm      {per_depth[m].pow(2).mean().sqrt().item():.4f}")

    # ---------- 2. water over a whole week ----------
    miss_h = torch.cat([water_balance_miss(pred[j:j + 64], p[j:j + 64], wi[j:j + 64], et[j:j + 64]) for j in range(0, len(p), 64)])
    week_mm = miss_h.sum(1).abs() * 10                       # cm -> mm, per week, sign kept until abs
    in_mm = wi.sum(1) * 10
    print("\n2. WATER BALANCE over a whole week (water the FNO invents or loses)")
    print(f"   typical week  {week_mm.median().item():.2f} mm   (water in that week: median {in_mm.median().item():.1f} mm)")
    print(f"   worst 5%      {week_mm.quantile(0.95).item():.2f} mm or more")
    print(f"   as share of water in: median {(week_mm / in_mm.clamp(min=1)).median().item() * 100:.1f} %")

    # ---------- 3. speed ----------
    k = min(1000, len(p))
    if dev == "cuda":
        torch.cuda.synchronize()
    t0 = time.time()
    predict(model, wi[:k], et[:k], p[:k], th[:k, 0], bs=128)
    if dev == "cuda":
        torch.cuda.synchronize()
    dt = time.time() - t0
    print(f"\n3. SPEED: FNO did {k} weeks in {dt:.2f} s = {dt / k * 1000:.1f} ms per week")
    print("   (our solver: about 2 s per week per CPU core, from your 20k run)")

    # ---------- 4. soil recovery ----------
    nf = min(a.n_fit, len(p))
    probe = [int(round((c - 5) / 2.5)) for c in PROBE_CM]
    lo, hi = PARAM_LO.to(dev), PARAM_HI.to(dev)
    obs = th[:nf][:, :, probe]                               # what 4 probes would measure
    x = torch.zeros(nf, 12, device=dev, requires_grad=True)  # start: middle of every limit
    opt = torch.optim.Adam([x], lr=0.05)
    guess = lo + (hi - lo) * torch.sigmoid(x.detach())
    for step in range(a.steps):
        pf = lo + (hi - lo) * torch.sigmoid(x)
        out = model(wi[:nf], et[:nf], pf, th[:nf, 0])
        loss = ((out[:, :, probe] - obs) ** 2).mean()
        opt.zero_grad()
        loss.backward()
        opt.step()
        if step % 100 == 0 or step == a.steps - 1:
            print(f"   fit step {step:3d}: probe error {loss.sqrt().item():.4f} m3/m3", flush=True)
    pf = (lo + (hi - lo) * torch.sigmoid(x)).detach()
    full_fit = predict(model, wi[:nf], et[:nf], pf, th[:nf, 0])
    full_guess = predict(model, wi[:nf], et[:nf], guess, th[:nf, 0])
    hidden = [i for i in range(th.shape[2]) if i not in probe]
    print(f"\n4. SOIL RECOVERY on {nf} kept-aside weeks (soil hidden, only 4 probes shown)")
    print(f"   {'':34s}{'all depths':>12s}{'depths no probe saw':>22s}")
    for name, q in (("soil = middle guess (no fitting)", full_guess), ("soil = found from the 4 probes", full_fit),
                    ("soil = true numbers (best case)", pred[:nf])):
        print(f"   {name:34s}{rmse(q, th[:nf]):12.4f}{rmse(q[:, :, hidden], th[:nf][:, :, hidden]):22.4f}")
    err_fit = ((pf - p[:nf]) / (hi - lo)).abs().mean(0)
    err_guess = ((guess - p[:nf]) / (hi - lo)).abs().mean(0)
    print("\n   soil numbers: average miss as a share of each number's full range (0 % = exact)")
    print("   " + "  ".join(f"{nm}" for nm in NAMES))
    print("   found: " + "  ".join(f"{v * 100:.0f}%" for v in err_fit.tolist()))
    print("   guess: " + "  ".join(f"{v * 100:.0f}%" for v in err_guess.tolist()))
