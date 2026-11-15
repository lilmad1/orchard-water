"""1D Richards equation (mixed form, Celia 1990 Picard scheme), depth z positive down, cm and hours.
Top node (5 cm) follows a measured 5 cm soil water series (Dirichlet). Bottom: free drainage.
Root uptake = f * measured ET * root weight(z) * stress factor."""
import numpy as np, pandas as pd
from scipy.linalg import solve_banded

DZ=2.5; Z=np.arange(5,120+1e-9,DZ); N=len(Z)
OBS_DEPTHS=[20,30,60,80]; OBS_IDX=[int(round((d-5)/DZ)) for d in OBS_DEPTHS]

def vg(p, layer):
    return p[layer]

class Soil:
    def __init__(self, layers, split=12.5):
        # layers: list of dicts with tr, ts, a (1/cm), n, ks (cm/h)
        L=[layers[0] if z<split else layers[-1] for z in Z]
        self.tr=np.array([l['tr'] for l in L]); self.ts=np.array([l['ts'] for l in L])
        self.a=np.array([l['a'] for l in L]); self.n=np.array([l['n'] for l in L])
        self.ks=np.array([l['ks'] for l in L]); self.m=1-1/self.n
    def se(self,h):
        return np.where(h<0,(1+(self.a*np.abs(h))**self.n)**(-self.m),1.0)
    def theta(self,h): return self.tr+(self.ts-self.tr)*self.se(h)
    def K(self,h):
        s=np.clip(self.se(h),1e-12,1); return self.ks*np.sqrt(s)*(1-(1-s**(1/self.m))**self.m)**2
    def C(self,h):
        ah=self.a*np.abs(h)
        c=(self.ts-self.tr)*self.a*self.n*self.m*ah**(self.n-1)*(1+ah**self.n)**(-self.m-1)
        return np.where(h<0,c,1e-7)+1e-7
    def h_of_theta(self,th,i=slice(None)):
        tr,ts,a,n,m=self.tr[i],self.ts[i],self.a[i],self.n[i],self.m[i]
        se=np.clip((th-tr)/(ts-tr),1e-6,0.999)
        return -((se**(-1/m)-1)**(1/n))/a

def run(soil, top_theta, et_cmh, theta0, f=1.0, lam=40.0, h50=-1500.0, zroot=100.0, budget=None):
    """top_theta: array per hour (5 cm VWC). et_cmh: array per hour (cm/h). Returns theta at all nodes per hour (T,N)."""
    T=len(top_theta)
    w=np.where((Z>5)&(Z<=zroot),np.exp(-Z/lam),0.0); w=w/(w.sum()*DZ)
    h=soil.h_of_theta(np.clip(theta0,soil.tr+1e-4,soil.ts-1e-4))
    out=np.empty((T,N)); out[0]=soil.theta(h)
    htop=soil.h_of_theta(np.clip(top_theta,soil.tr[0]+1e-4,soil.ts[0]-1e-4),0)
    import time as _t; t_start=_t.time()
    for t in range(1,T):
        if budget and t%500==0 and _t.time()-t_start>budget: raise RuntimeError('too slow')
        dt_total=1.0; done=0.0; dt=1.0
        while done<dt_total-1e-9:
            hn=h.copy(); thn=soil.theta(hn); ok=False
            hm=hn.copy(); hm[0]=htop[t]
            for it in range(30):
                K=soil.K(hm); Kf=0.5*(K[1:]+K[:-1])  # interfaces i+1/2, len N-1
                C=soil.C(hm); thm=soil.theta(hm)
                al=1/(1+(hm/h50)**3)*(hm<0)+1.0*(hm>=0)
                S=f*et_cmh[t]*w*al
                # build tridiagonal for interior + bottom
                A=np.zeros((3,N)); b=np.zeros(N)
                A[1,0]=1; b[0]=htop[t]
                i=np.arange(1,N-1)
                kup=Kf[i-1]; kdn=Kf[i]
                A[1,i]=C[i]/dt+(kup+kdn)/DZ**2
                A[2,i-1]=-kup/DZ**2   # sub-diagonal: coefficient of h[i-1] in row i
                A[0,i+1]=-kdn/DZ**2   # super-diagonal: coefficient of h[i+1] in row i
                b[i]=C[i]*hm[i]/dt-(thm[i]-thn[i])/dt+(kup-kdn)/DZ-S[i]
                # bottom node: flux in from above, free drainage out (q=K)
                j=N-1; kup=Kf[j-1]
                A[1,j]=C[j]/dt+kup/DZ**2*2
                A[2,j-1]=-kup/DZ**2*2
                b[j]=C[j]*hm[j]/dt-(thm[j]-thn[j])/dt+(kup-K[j])/DZ*2-S[j]
                hnew=solve_banded((1,1),A,b)
                hnew=np.minimum(hnew,0.0)
                if np.max(np.abs(hnew-hm))<0.5: hm=hnew; ok=True; break
                hm=hnew
            if ok or dt<1/64:
                h=hm; done+=dt; dt=min(dt*2,dt_total-done) if done<dt_total else dt
            else:
                dt/=2
        out[t]=soil.theta(h)
    return out

def load(csv_path, t0=None, t1=None):
    """Read your own hourly probe CSV. Columns: `time` (ISO datetime, hourly),
    `theta_top` (water content at 5 cm, m3/m3) and `et_mm_day` (daily ET in mm/day,
    repeated on every hour of the day, or only given once per day and filled forward).
    Returns (table, top_theta per hour, ET in cm/h) ready for run()."""
    hrl=pd.read_csv(csv_path,parse_dates=['time'],index_col='time').sort_index()
    hrl=hrl.interpolate(limit=3)[t0:t1]
    top=hrl['theta_top'].values
    eth=hrl['et_mm_day'].ffill().values/10/24
    return hrl, top, eth
