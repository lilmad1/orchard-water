"""1D Richards equation in PyTorch, so the soil numbers can be learned by gradient descent.

Same physics as richards.py (the old numpy solver):
- soil column from 5 to 120 cm, one node every 2.5 cm, depth z positive down
- top node (5 cm) follows the measured 5 cm soil water
- bottom: free drainage (water leaves at the speed K)
- roots take a share f of the measured ET, more near the top (lam = how deep)

What is new: every step is a PyTorch operation, so PyTorch can work out how the
error changes when a soil number changes (the gradient). We run many
windows of time side by side (a "batch") to make it faster.

Units: cm and hours. Water content theta in m3/m3.
"""
import torch

torch.set_default_dtype(torch.float64)  # soil physics needs precise numbers

DZ = 2.5
Z = torch.arange(5.0, 120.0 + 1e-9, DZ)  # node depths (cm)
N = len(Z)
OBS_DEPTHS = [20, 30, 60, 80]
OBS_IDX = [int(round((d - 5) / DZ)) for d in OBS_DEPTHS]
SPLIT_CM = 12.5  # top layer above this depth, bottom layer below


# ---------- Part 1: the soil curves (van Genuchten) ----------
# For each node: tr = driest water content, ts = wettest, a and n = shape,
# ks = how fast water flows when the soil is full (cm/h).

def soil_arrays(layers):
    """layers: list of 2 dicts of tensors -> one tensor per soil number, one value per node."""
    top = Z < SPLIT_CM
    return {k: torch.where(top, layers[0][k], layers[1][k]) for k in ("tr", "ts", "a", "n", "ks")}


def se(s, h):
    """Effective saturation: 0 = driest, 1 = full. h = suction head (cm, negative when not full)."""
    m = 1 - 1 / s["n"]
    ah = s["a"] * torch.clamp(-h, min=1e-6)
    return (1 + ah ** s["n"]) ** (-m)


def theta(s, h):
    return s["tr"] + (s["ts"] - s["tr"]) * se(s, h)


def conductivity(s, h):
    """How fast water moves at suction h (Mualem)."""
    m = 1 - 1 / s["n"]
    e = torch.clamp(se(s, h), 1e-12, 1.0)
    return s["ks"] * e.sqrt() * (1 - (1 - e ** (1 / m)) ** m) ** 2


def capacity(s, h):
    """d theta / d h: how much water content changes per cm of suction."""
    m = 1 - 1 / s["n"]
    ah = s["a"] * torch.clamp(-h, min=1e-6)
    c = (s["ts"] - s["tr"]) * s["a"] * s["n"] * m * ah ** (s["n"] - 1) * (1 + ah ** s["n"]) ** (-m - 1)
    return c + 1e-7


def h_of_theta(s, th):
    """The other way round: suction head from water content."""
    m = 1 - 1 / s["n"]
    e = torch.clamp((th - s["tr"]) / (s["ts"] - s["tr"]), 1e-6, 0.999)
    return -((e ** (-1 / m) - 1) ** (1 / s["n"])) / s["a"]


# ---------- Part 2: one time step ----------

def step(s, h, top_h, sink, dt, iters=2, top_flux=None):
    """Move the profile h (batch, N) forward by dt hours.

    Implicit scheme (Celia 1990 mixed form, Picard iterations): solve a small
    linear system for the new h. top_h: (batch,) suction at 5 cm. sink: (batch, N)
    root uptake (1/h).
    top_flux: (batch,) water coming in at the top (cm/h), e.g. drip + rain from a
    water meter. If given, it replaces top_h: the top node is then free and its
    water content follows from the water balance of the top half-slice.
    """
    B = h.shape[0]
    th_old = theta(s, h)
    hm = h.clone()
    if top_flux is None:
        hm[:, 0] = top_h
    i = torch.arange(1, N - 1)
    for _ in range(iters):
        K = conductivity(s, hm)
        Kf = 0.5 * (K[:, 1:] + K[:, :-1])  # K between nodes
        C = capacity(s, hm)
        thm = theta(s, hm)
        A = torch.zeros(B, N, N)
        b = torch.zeros(B, N)
        if top_flux is None:  # top node fixed to the measured 5 cm sensor
            A[:, 0, 0] = 1.0
            b[:, 0] = top_h
        else:  # top half-slice: water in from the meter, water out to the slice below
            k0 = Kf[:, 0]
            A[:, 0, 0] = C[:, 0] / dt + 2 * k0 / DZ**2
            A[:, 0, 1] = -2 * k0 / DZ**2
            b[:, 0] = (C[:, 0] * hm[:, 0] / dt - (thm[:, 0] - th_old[:, 0]) / dt
                       + 2 * (top_flux - k0) / DZ - sink[:, 0])
        kup, kdn = Kf[:, i - 1], Kf[:, i]
        A[:, i, i] = C[:, i] / dt + (kup + kdn) / DZ**2
        A[:, i, i - 1] = -kup / DZ**2
        A[:, i, i + 1] = -kdn / DZ**2
        b[:, i] = C[:, i] * hm[:, i] / dt - (thm[:, i] - th_old[:, i]) / dt + (kup - kdn) / DZ - sink[:, i]
        j = N - 1  # bottom node: water in from above, free drainage out
        kup = Kf[:, j - 1]
        A[:, j, j] = C[:, j] / dt + 2 * kup / DZ**2
        A[:, j, j - 1] = -2 * kup / DZ**2
        b[:, j] = C[:, j] * hm[:, j] / dt - (thm[:, j] - th_old[:, j]) / dt + 2 * (kup - K[:, j]) / DZ - sink[:, j]
        hm = torch.clamp(torch.linalg.solve(A, b), max=0.0)
    return hm


# ---------- Part 3: run a whole period ----------

def root_weights(lam, zroot=100.0):
    w = torch.where((Z > 5) & (Z <= zroot), torch.exp(-Z / lam), torch.zeros(()))
    return w / (w.sum() * DZ)


def run(s, top_theta, et_cmh, theta0, f, lam, substeps=4, h50=-1500.0):
    """top_theta, et_cmh: (batch, T) hourly. theta0: (batch, N) start profile.
    Returns water content at the 4 probe depths, (batch, T, 4)."""
    B, T = top_theta.shape
    w = root_weights(lam)
    top0 = {k: v[0] for k, v in s.items()}
    h = h_of_theta(s, torch.clamp(theta0, s["tr"] + 1e-4, s["ts"] - 1e-4))
    top_h = h_of_theta(top0, torch.clamp(top_theta, top0["tr"] + 1e-4, top0["ts"] - 1e-4))
    out = [theta(s, h)[:, OBS_IDX]]
    dt = 1.0 / substeps
    for t in range(1, T):
        for _ in range(substeps):
            stress = 1 / (1 + (h / h50) ** 3)  # roots take less when the soil is very dry
            sink = f * et_cmh[:, t:t + 1] * w * stress
            h = step(s, h, top_h[:, t], sink, dt)
        out.append(theta(s, h)[:, OBS_IDX])
    return torch.stack(out, dim=1)


# ---------- Part 4: the soil numbers we learn ----------
# Each number lives inside limits (same limits as the old fit). We learn a free
# number x and squeeze it into the limits with a sigmoid, so it can never leave them.
LIMITS = {  # per layer
    "tr": (0.0, 0.06), "ts": (0.30, 0.45), "log10_a": (-2.7, -1.0),
    "n": (1.2, 3.5), "log10_ks": (-1.5, 1.6),
}
LIMITS_ROOT = {"f": (0.1, 3.0), "lam": (10.0, 200.0)}


def squeeze(x, lo, hi):
    return lo + (hi - lo) * torch.sigmoid(x)


def unsqueeze(v, lo, hi):
    p = (v - lo) / (hi - lo)
    p = min(max(p, 1e-4), 1 - 1e-4)
    return torch.log(torch.tensor(p / (1 - p)))


class SoilParams(torch.nn.Module):
    def __init__(self, start=None):
        super().__init__()
        start = start or {
            "layers": [dict(tr=0.03, ts=0.38, log10_a=-1.8, n=2.0, log10_ks=0.6)] * 2,
            "f": 0.5, "lam": 30.0,
        }
        self.x = torch.nn.ParameterDict()
        for k, layer in enumerate(start["layers"]):
            for name, (lo, hi) in LIMITS.items():
                self.x[f"{name}_{k}"] = torch.nn.Parameter(unsqueeze(layer[name], lo, hi))
        for name, (lo, hi) in LIMITS_ROOT.items():
            self.x[name] = torch.nn.Parameter(unsqueeze(start[name], lo, hi))

    def values(self):
        v = {}
        for key, p in self.x.items():
            name = key.rsplit("_", 1)[0] if key[-1].isdigit() else key
            lo, hi = LIMITS.get(name) or LIMITS_ROOT[name]
            v[key] = squeeze(p, lo, hi)
        return v

    def soil(self):
        v = self.values()
        layers = [dict(tr=v[f"tr_{k}"], ts=v[f"ts_{k}"], a=10 ** v[f"log10_a_{k}"],
                       n=v[f"n_{k}"], ks=10 ** v[f"log10_ks_{k}"]) for k in range(2)]
        return soil_arrays(layers), v["f"], v["lam"]
