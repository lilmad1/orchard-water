"""The soil water part of the engine, as a swappable piece.

Why this file exists: This project may later replace the simple "bucket" soil model
with an FNO/PINO neural operator later: a Richards-equation soil
model with soil parameters learned from probe data, and later a pseudo-3D
model of the wet bulb under a dripper. That FNO/PINO neural operator is NOT built here. This file
only draws the line between "how much water does the crop want" (crop demand,
see kc.py and fao56.py) and "what happens to the water in the soil" (this
file), so the FNO/PINO neural operator can later drop in without touching the Kc code or the
scheduling code.

The deal every soil model must keep (the SoilWaterModel interface below):

- Input each time step: the crop's potential water use, split into
  transpiration (through the tree) and evaporation (from wet soil), plus the
  water that arrived (effective rain now, irrigation later).
- Output each time step: the root-zone depletion in mm (how much water it
  would take to bring the root zone back to full, "field capacity"), the
  crop's actual water use, and the water lost below the roots (drainage).
- taw_mm: the total usable water the root zone can hold. The scheduler
  (water_balance.irrigation_deadline) only looks at depletion, taw_mm and
  raw_mm (the stress-free part, p x TAW, still worked out in weekly.py from
  fao56.py), so any model that gives depletion and taw_mm can drive the
  weekly advice. An FNO/PINO neural operator may later give its own stress limit from soil
  suction instead of p x TAW; that would be a new, separate decision.

Depletion in mm is the shared language. For the bucket it is the bucket's own
number. For a Richards-based neural operator model it would be computed from the water content
profile: the sum over root-zone layers of (field-capacity content minus
current content) times layer thickness.

Built for PyTorch / NVIDIA PhysicsNeMo later: step() is a pure function. It takes the old
state and returns a new state, and changes nothing else. The state is a small NamedTuple
of numbers. That is the shape a PyTorch training loop needs. The bucket below uses plain
Python min/max; a PyTorch copy would use torch.clamp and could be differentiated, which
is what lets an FNO/PINO neural operator learn soil parameters by gradient descent.
PhysicsNeMo (built on PyTorch) is planned for the 2D/3D drip twin; the first 1D model can
be plain PyTorch. Neither is added to the project now.

Not covered yet, on purpose (needed when the FNO/PINO neural operator arrives):
- state.py saves only one depletion number per field. A Richards model has a
  whole water content profile, so state.py will need to save that too.
- The thin surface layer that dries after wetting (De, FAO-56 Eq. 77) still
  lives in water_balance.py. With a Richards model, surface evaporation comes
  from the top of the soil profile instead, so the FNO/PINO neural operator would take that over.
- irrigation_deadline() checks "would a full refill the hour before still
  cross the limit?" with the bucket equation directly.
"""

from dataclasses import dataclass
from typing import NamedTuple, Protocol, TypeVar


class CropDemand(NamedTuple):
    """What the crop would use in one time step if the soil had enough water (mm).

    total_mm: all of it (FAO-56's ETc for the step).
    evaporation_mm: the part that would evaporate from wet soil. The rest,
        transpiration_mm, is what the tree would pull through its roots.

    The bucket only needs the total. A Richards-based neural operator model needs the two parts
    apart: transpiration is taken out where the roots are, evaporation at the
    top. The total is stored as given (not re-added from the parts), so the
    bucket gets exactly the number the engine computed. A fitted monthly Kc
    (kc.py) measures both parts together, so in that mode evaporation_mm is 0
    and all of it counts as transpiration (see
    water_balance.run_forecast_period).
    """

    total_mm: float
    evaporation_mm: float = 0.0

    @property
    def transpiration_mm(self) -> float:
        return self.total_mm - self.evaporation_mm


class SoilStepResult(NamedTuple):
    """What one time step of a soil model reports (all mm)."""

    depletion_mm: float  # root-zone depletion at the end of the step
    actual_et_mm: float  # water the crop actually used this step
    drainage_mm: float  # water lost below the root zone this step


class BucketState(NamedTuple):
    """The bucket's whole memory: one number."""

    depletion_mm: float


StateT = TypeVar("StateT")


class SoilWaterModel(Protocol[StateT]):
    """The interface every soil model keeps. See the module docstring.

    StateT is the model's own memory: one number for the bucket, a water
    content profile for a Richards-based neural operator model. Code outside the soil model
    never looks inside it; it only passes it back into the next step().
    """

    @property
    def taw_mm(self) -> float: ...

    def initial_state(self, depletion_mm: float) -> StateT: ...

    def step(
        self, state: StateT, demand: CropDemand, water_in_mm: float
    ) -> tuple[StateT, SoilStepResult]: ...


@dataclass(frozen=True)
class BucketModel:
    """FAO-56's root-zone bucket (Eq. 85 and 88): the reference soil model.

    The root zone is one tank holding taw_mm of usable water. Depletion goes up
    with crop water use and down with water arriving. If more water arrives than
    the tank has room for, the extra drains away below the roots. Depletion
    never goes above taw_mm (the tank cannot hold less than nothing).

    Like the earlier built-in engine, the bucket does not reduce water use
    when the soil gets dry (FAO-56's Ks is kept at 1). That is fine for
    scheduling, because the advice is built to water before depletion
    reaches raw_mm, where FAO-56 says stress can start. So actual_et_mm
    equals the demand.

    The arithmetic is the same as water_balance.step_one_period, which the
    engine used before the soil model became swappable (a test checks they
    agree).
    """

    taw_mm: float

    def __post_init__(self) -> None:
        if not self.taw_mm > 0:
            raise ValueError("taw_mm must be above 0")

    def initial_state(self, depletion_mm: float) -> BucketState:
        return BucketState(depletion_mm=depletion_mm)

    def step(
        self, state: BucketState, demand: CropDemand, water_in_mm: float
    ) -> tuple[BucketState, SoilStepResult]:
        etc_mm = demand.total_mm
        before_limits = state.depletion_mm - water_in_mm + etc_mm
        drainage_mm = max(0.0, -before_limits)
        depletion_mm = min(self.taw_mm, max(0.0, before_limits))
        return BucketState(depletion_mm), SoilStepResult(depletion_mm, etc_mm, drainage_mm)
