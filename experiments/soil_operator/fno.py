"""The FNO (Fourier Neural Operator) soil model: a fast copy of our physics.

What goes in, for one week of one block, on a grid of 168 hours x 47 depths:
  - water in at the top each hour (drip + rain, from the meter)  -> spread over all depths
  - ET each hour                                                  -> spread over all depths
  - the 12 soil and root numbers                                  -> the same at every point
  - the start profile (water content at each depth at hour 0)     -> the same at every hour
  - where we are: time and depth (0..1)
What comes out: the water content at every hour and depth.

How it works, in 3 steps:
  1. LIFT: each grid point's 17 inputs -> 32 numbers ("channels"), with one small layer.
  2. FOURIER LAYERS (x4): each layer looks at the WHOLE week at once. It splits the
     pattern into waves (Fourier), keeps the slow waves (daily rhythm, pulses), weights
     them with learned numbers, and turns them back. Plus a normal local layer.
     This is why it is good at things that repeat, like daily drip.
  3. PROJECT: 32 numbers -> 1 number per grid point = how full the soil is (0..1).
     We turn that into water content with the soil's own dry and full limits
     (tr, ts), so the answer can never be drier than dry or wetter than full.
"""
import math
import torch
import torch.nn as nn
import torch.nn.functional as F

# Limits used to scale the 12 numbers to 0..1 before they go in (same order as synth.py)
PARAM_LO = torch.tensor([0.0, 0.30, -2.7, 1.2, -1.5] * 2 + [0.3, 10.0])
PARAM_HI = torch.tensor([0.10, 0.50, -1.0, 3.5, 1.6] * 2 + [1.0, 60.0])  # = synth.py WIDE_LO/WIDE_HI
SPLIT_IDX = 3  # depths 5, 7.5, 10 cm are the top layer (< 12.5 cm), the rest bottom layer


class SpectralConv2d(nn.Module):
    """The Fourier part: FFT -> keep the lowest `modes` waves -> multiply by learned weights -> inverse FFT."""

    def __init__(self, ch, modes_t, modes_z):
        super().__init__()
        self.mt, self.mz = modes_t, modes_z
        scale = 1 / (ch * ch)
        # complex weights for low positive and low negative time-frequencies
        self.w1 = nn.Parameter(scale * torch.randn(ch, ch, modes_t, modes_z, dtype=torch.cfloat))
        self.w2 = nn.Parameter(scale * torch.randn(ch, ch, modes_t, modes_z, dtype=torch.cfloat))

    def forward(self, x):                                  # x: (B, ch, T, Z)
        B, C, T, Z = x.shape
        xf = torch.fft.rfft2(x)                             # waves: (B, ch, T, Z//2+1)
        out = torch.zeros_like(xf)
        mt, mz = self.mt, self.mz
        out[:, :, :mt, :mz] = torch.einsum("bitz,iotz->botz", xf[:, :, :mt, :mz], self.w1)
        out[:, :, -mt:, :mz] = torch.einsum("bitz,iotz->botz", xf[:, :, -mt:, :mz], self.w2)
        return torch.fft.irfft2(out, s=(T, Z))


class FNO(nn.Module):
    def __init__(self, width=32, modes_t=24, modes_z=12, layers=4, pad_t=24):
        super().__init__()
        self.pad_t = pad_t  # the week does not repeat, so we pad time to stop end and start mixing
        self.lift = nn.Linear(17, width)
        self.spec = nn.ModuleList([SpectralConv2d(width, modes_t, modes_z) for _ in range(layers)])
        self.local = nn.ModuleList([nn.Conv2d(width, width, 1) for _ in range(layers)])
        self.proj = nn.Sequential(nn.Linear(width, 64), nn.GELU(), nn.Linear(64, 1))

    @staticmethod
    def make_inputs(water_in, et, params, theta0):
        """Build the (B, T, Z, 17) input grid. water_in, et: (B, T) cm/h; params: (B, 12); theta0: (B, Z)."""
        B, T = water_in.shape
        Z = theta0.shape[1]
        pn = (params - PARAM_LO.to(params)) / (PARAM_HI - PARAM_LO).to(params)
        g = lambda v: v[:, :, None, None].expand(B, T, Z, 1)
        t = torch.linspace(0, 1, T, device=params.device)[None, :, None, None].expand(B, T, Z, 1)
        z = torch.linspace(0, 1, Z, device=params.device)[None, None, :, None].expand(B, T, Z, 1)
        return torch.cat([
            g(water_in * 10), g(et * 100),                       # scaled to roughly 0..1
            pn[:, None, None, :].expand(B, T, Z, 12),
            (theta0 * 3)[:, None, :, None].expand(B, T, Z, 1),
            t, z,
        ], dim=-1)

    def forward(self, water_in, et, params, theta0):
        x = self.lift(self.make_inputs(water_in, et, params, theta0))   # (B, T, Z, width)
        x = x.permute(0, 3, 1, 2)                                       # (B, width, T, Z)
        x = F.pad(x, (0, 0, 0, self.pad_t))
        for k, (sp, lo) in enumerate(zip(self.spec, self.local)):
            x = sp(x) + lo(x)
            if k < len(self.spec) - 1:
                x = F.gelu(x)
        x = x[:, :, : water_in.shape[1], :].permute(0, 2, 3, 1)        # drop padding
        fullness = torch.sigmoid(self.proj(x)[..., 0])                  # (B, T, Z), 0..1
        tr, ts = soil_limits(params, theta0.shape[1])
        return tr[:, None, :] + (ts - tr)[:, None, :] * fullness


def soil_limits(params, Z):
    """Dry (tr) and full (ts) water content at each depth, from the 12 numbers."""
    top = torch.arange(Z, device=params.device) < SPLIT_IDX
    tr = torch.where(top, params[:, 0:1], params[:, 5:6])
    ts = torch.where(top, params[:, 1:2], params[:, 6:7])
    return tr, ts
