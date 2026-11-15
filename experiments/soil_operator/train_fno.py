"""Train the FNO on the synthetic weeks (from synth.py).

Loss = data part + physics part:
  - data:    how far the FNO's water content is from the solver's, at every hour and depth
  - physics: the water balance of the whole column, every hour:
             change in stored water = water in - root uptake - drainage at the bottom
             The FNO is punished if its answer breaks this, even where the data
             would let it. That is the "physics-informed" part (PINO).

15 % of the weeks are kept aside (never trained on) to check the FNO honestly.

Usage (on your GPU):
  python train_fno.py --data synth_train.pt --epochs 200
"""
import argparse, math, time
import torch
from fno import FNO, soil_limits

DZ = 2.5
H50 = -1500.0


def layer_values(params, Z, i):
    """One soil number (index i within a layer: 0 tr, 1 ts, 2 log10 a, 3 n, 4 log10 ks) at each depth."""
    top = torch.arange(Z, device=params.device) < 3
    return torch.where(top, params[:, i:i + 1], params[:, 5 + i:6 + i])


def water_balance_miss(theta, params, water_in, et):
    """Per hour: (stored change) - (in - roots - drainage), in cm. Zero for perfect physics."""
    B, T, Z = theta.shape
    tr, ts = soil_limits(params, Z)
    a = 10 ** layer_values(params, Z, 2)
    n = layer_values(params, Z, 3)
    ks = 10 ** layer_values(params, Z, 4)
    m = 1 - 1 / n
    se = ((theta - tr[:, None]) / (ts - tr)[:, None]).clamp(1e-6, 0.999)
    h = -((se ** (-1 / m[:, None]) - 1) ** (1 / n[:, None])) / a[:, None]   # suction (cm)
    # drainage at the bottom = conductivity there
    sb, mb = se[..., -1], m[:, None, -1]
    q_bottom = ks[:, None, -1] * sb.sqrt() * (1 - (1 - sb ** (1 / mb)) ** mb) ** 2
    # root uptake: share f of ET, spread over depth with exp(-z/lam), less when very dry
    z = 5 + DZ * torch.arange(Z, device=theta.device)
    lam = params[:, 11:12]
    w = torch.where((z > 5) & (z <= 100), torch.exp(-z / lam), torch.zeros(()))
    w = w / (w.sum(-1, keepdim=True) * DZ)
    dzw = torch.full((Z,), DZ, device=theta.device)
    dzw[0] = dzw[-1] = DZ / 2
    stress = 1 / (1 + (h / H50) ** 3)
    roots = (params[:, 10, None, None] * et[:, :, None] * w[:, None, :] * stress * dzw).sum(-1)
    stored = (theta * dzw).sum(-1)
    return (stored[:, 1:] - stored[:, :-1]) - (water_in[:, 1:] - roots[:, 1:] - q_bottom[:, 1:])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="synth_train.pt")
    ap.add_argument("--epochs", type=int, default=200)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--physics", type=float, default=1.0, help="weight of the water-balance part (0 = data only)")
    ap.add_argument("--out", default="fno.pt")
    ap.add_argument("--init", default=None, help="start from an already trained FNO (e.g. fno_20k.pt) instead of random numbers")
    a = ap.parse_args()

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    torch.manual_seed(0)
    d = torch.load(a.data)
    n = len(d["params"])
    perm = torch.randperm(n)
    n_val = max(1, int(0.15 * n))
    val, train = perm[:n_val], perm[n_val:]
    put = lambda idx: [d[k][idx].to(dev) for k in ("water_in", "et", "params", "theta")]
    tr_set, va_set = put(train), put(val)
    print(f"device {dev}: {len(train)} training weeks, {len(val)} kept aside")

    model = FNO().to(dev)
    if a.init:
        model.load_state_dict(torch.load(a.init, map_location=dev))
        print("starting from", a.init)
    print("trainable numbers in the FNO:", sum(p.numel() for p in model.parameters()))
    opt = torch.optim.Adam(model.parameters(), lr=a.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, a.epochs)
    best = 1e9
    for epoch in range(a.epochs):
        t0 = time.time()
        model.train()
        order = torch.randperm(len(train), device=dev)
        for i in range(0, len(train), a.batch):
            wi, et, p, th = (x[order[i:i + a.batch]] for x in tr_set)
            pred = model(wi, et, p, th[:, 0])
            data_loss = ((pred - th) ** 2).mean() / 0.01 ** 2           # in units of (0.01 m3/m3)^2
            phys_loss = (water_balance_miss(pred, p, wi, et) ** 2).mean() / 0.01 ** 2  # (0.1 mm/h)^2
            loss = data_loss + a.physics * phys_loss
            opt.zero_grad()
            loss.backward()
            opt.step()
        sched.step()
        model.eval()
        with torch.no_grad():
            wi, et, p, th = va_set
            pred = torch.cat([model(wi[j:j + 64], et[j:j + 64], p[j:j + 64], th[j:j + 64, 0]) for j in range(0, len(p), 64)])
            val_rmse = ((pred - th) ** 2).mean().sqrt().item()
            val_bal = water_balance_miss(pred, p, wi, et).abs().mean().item() * 10   # mm/h
        if val_rmse < best:
            best = val_rmse
            torch.save(model.state_dict(), a.out)
        print(f"epoch {epoch:3d}  train data {data_loss.item():.3f} phys {phys_loss.item():.3f}  |  "
              f"kept-aside error {val_rmse:.4f} m3/m3, water balance miss {val_bal:.3f} mm/h  ({time.time() - t0:.1f} s)", flush=True)
    print(f"best kept-aside error {best:.4f} m3/m3, saved {a.out}")
