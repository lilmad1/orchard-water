"""Turn a Landsat temperature picture into surface-temperature numbers for one field.

Surface temperature is how hot the ground and the leaves are, measured from space
(in kelvin, written K; 0 C is 273.15 K). It is NOT the air temperature. Hot leaves
can mean a plant is closing its pores because it lacks water, but heat also depends
on sun, wind and soil. This is temperature only. It is NOT a measure of water stress.

This module does no internet work. landsat.py fetches the picture; this file checks
it and does the math. Grids are plain lists of rows, like in ndvi.py.

How sharp it is: the Landsat heat sensor really sees squares of about 100 m. The
files are delivered on a 30 m grid, but those numbers are smoothly blended from the
100 m readings, so a 30 m grid looks more detailed than it is. A 1 ha field holds
about one real reading. That is why the report gives one number per field and no map.
"""

from dataclasses import dataclass, field

import numpy as np

# The quality layer (QA_PIXEL) packs yes/no flags into the bits of one whole number.
# Bit numbers are from the band description in the Planetary Computer catalogue,
# read on 2026-10-04: 0 fill, 1 dilated cloud, 2 cirrus, 3 cloud, 4 cloud shadow,
# 5 snow or ice, 6 clear, 7 water. A fully clear square reads 21824 (bit 6 set, and
# the "low confidence" values in bits 8 to 15).
FILL_FLAG = 1 << 0
DILATED_CLOUD_FLAG = 1 << 1
CIRRUS_FLAG = 1 << 2
CLOUD_FLAG = 1 << 3
SHADOW_FLAG = 1 << 4
SNOW_FLAG = 1 << 5
CLEAR_FLAG = 1 << 6
WATER_FLAG = 1 << 7

# Each of cloud, shadow, snow and cirrus also has a 2-bit "confidence" number next to its
# flag, at bits 8-9 (cloud), 10-11 (shadow), 12-13 (snow) and 14-15 (cirrus). Values: 0
# none, 1 low, 3 high, and 2 medium for cloud only (the catalogue's own band description
# calls 2 "reserved" for the other three, checked 2026-10-04). The clear flag (bit 6)
# only says the cloud and dilated-cloud flags are off; it does NOT say the satellite is
# sure. A square the satellite thinks is medium or high likely cloud can still carry the
# clear flag, so a confidence of 2 or more in any pair also drops the square.
CONFIDENCE_SHIFTS = (8, 10, 12, 14)
MEDIUM_CONFIDENCE = 2

# The strict rule: a square counts only if the clear flag is set, none of these flags
# is set, and no confidence pair is medium or high. Why strict: cloud and cloud
# shadow read too COLD, and a too-cold field looks unstressed, which could lead to
# too little water. That is the unsafe direction, so a doubtful square is dropped
# rather than kept. Water is dropped too (a reservoir is not the crop), as in the
# Sentinel-2 rule in ndvi.py.
# TODO(verify): flag meanings against the USGS Collection 2 Level-2 documentation.
BLOCKING_FLAGS = (
    FILL_FLAG | DILATED_CLOUD_FLAG | CIRRUS_FLAG | CLOUD_FLAG | SHADOW_FLAG | SNOW_FLAG | WATER_FLAG
)

# What counts as "cloud nearby" (see is_cloud_square): the cloud-related flags only. Water
# and snow do not mean cloud, and a missing neighbour is not treated as cloud.
CLOUD_RELATED_FLAGS = DILATED_CLOUD_FLAG | CIRRUS_FLAG | CLOUD_FLAG | SHADOW_FLAG
CLOUD_CONFIDENCE_SHIFTS = (8, 10, 14)  # cloud, shadow and cirrus; not snow

# A surface temperature outside this range cannot be real for a field and means a
# broken value. Such a square is dropped and counted. TODO(verify): the limits are
# a sanity range chosen here, not a documented valid range.
MIN_KELVIN = 200.0
MAX_KELVIN = 400.0

ZERO_CELSIUS_K = 273.15

# The size of the squares the heat sensor really sees, in metres.
SENSOR_SQUARE_M = 100.0


@dataclass(frozen=True)
class ThermalPicture:
    """One day's temperature picture as three grids of the same size (rows of squares).

    kelvin: surface temperature in kelvin for each square (0.0 where unusable).
    qa: the quality number for each square (see CLEAR_FLAG and BLOCKING_FLAGS).
    data_mask: 1 where the satellite had a usable temperature, 0 where not.
    qa_valid: 1 where the quality number is available, 0 where not. It is separate from
        data_mask on purpose: the satellite has known gaps in the temperature (Landsat
        Level-2 temperature is missing where a helper dataset has no data), and a cloud
        flag next to such a gap must still be seen.
    discarded_squares: squares that had a stored temperature but outside
        MIN_KELVIN to MAX_KELVIN (they show as data_mask 0).
    discarded: the same squares as a grid (1 where discarded, else 0), so a summary can
        count the ones inside the field outline. Empty for a picture built without it.
    """

    kelvin: list[list[float]]
    qa: list[list[int]]
    data_mask: list[list[int]]
    qa_valid: list[list[int]]
    discarded_squares: int = 0
    discarded: list[list[int]] = field(default_factory=list)


@dataclass(frozen=True)
class ThermalSummary:
    """Surface temperature of one field on one day.

    field_squares: grid squares whose centre lies inside the field outline.
    observed_squares: field squares with a usable temperature.
    clear_squares: observed squares that pass the strict cloud rule.
    coverage: observed_squares / field_squares (0 to 1).
    clear_share: clear_squares / observed_squares (0 to 1), or 0.0 if none observed.
    valid_share: clear_squares / field_squares (0 to 1).
    unknown_squares: squares ANYWHERE in the picture (field or margin) with no usable
        quality number (outside the file, or fill). There the satellite cannot say whether
        cloud is present, so the report skips the pass rather than guess.
    cloud_squares: squares ANYWHERE in the picture (the field and the margin around it)
        that are cloud, thin cirrus or cloud shadow (see is_cloud_square). A cold square
        next to cloud is itself too cold, because every value is blended from about
        100 m of ground, so any cloud in view makes the whole pass doubtful.
    mean_k, min_k, max_k: over the clear squares, in kelvin, or None if none.
    real_readings: how many 100 m sensor readings the field area is worth (the outline's
        area in m2 / 10,000 m2). Exactly 1 for a 1 ha field.
    discarded_field_squares: squares INSIDE the outline that had a stored temperature but
        outside the believable range (thrown out). Unlike the picture's discarded_squares,
        the margin is not counted.
    """

    field_squares: int
    observed_squares: int
    clear_squares: int
    coverage: float
    clear_share: float
    valid_share: float
    unknown_squares: int
    cloud_squares: int
    mean_k: float | None
    min_k: float | None
    max_k: float | None
    real_readings: float
    discarded_field_squares: int = 0


def to_celsius(kelvin: float) -> float:
    """Degrees Celsius from kelvin: C = K - 273.15."""
    return kelvin - ZERO_CELSIUS_K


def quality_available(qa: np.ndarray) -> np.ndarray:
    """True where a quality number holds real information about the ground.

    A number of 0 means the file has nothing there, and a number with the fill flag set
    means the square lies outside the picture: neither says anything about cloud. If a
    fill square were counted as available, it could block a real cloud flag from another
    scene of the same day.
    """
    available: np.ndarray = (qa != 0) & ((qa & FILL_FLAG) == 0)
    return available


def thermal_picture_from_bands(
    kelvin: np.ndarray,
    qa: np.ndarray,
    photographed: np.ndarray,
    qa_valid: np.ndarray | None = None,
) -> ThermalPicture:
    """Build a checked ThermalPicture from three same-size grids (rows x columns).

    kelvin: surface temperature in kelvin; NaN means missing.
    qa: the quality number per square, whole numbers only.
    photographed: True where the satellite stored a temperature for the square.
    qa_valid: True where the quality number is available, whether or not there is a
        temperature. If not given, a quality number of 0, or one with the fill flag set
        (outside the picture, not observed ground), is taken to mean "not available".
    A photographed square whose temperature is missing or outside MIN_KELVIN to
    MAX_KELVIN is left out of the picture (data_mask 0) and counted in
    discarded_squares, so the report can mention it.
    """
    if not np.issubdtype(qa.dtype, np.integer):
        raise ValueError("The quality layer must hold whole numbers.")
    with np.errstate(invalid="ignore"):  # comparing a missing value is fine here
        in_range = np.isfinite(kelvin) & (kelvin >= MIN_KELVIN) & (kelvin <= MAX_KELVIN)
    usable = photographed & in_range
    return ThermalPicture(
        kelvin=np.where(usable, kelvin, 0.0).astype(float).tolist(),
        qa=qa.astype(int).tolist(),
        data_mask=usable.astype(int).tolist(),
        qa_valid=(quality_available(qa) if qa_valid is None else qa_valid).astype(int).tolist(),
        discarded_squares=int((photographed & ~in_range).sum()),
        discarded=(photographed & ~in_range).astype(int).tolist(),
    )


def is_clear_square(qa: int, data_mask: int) -> bool:
    """True if there is a usable temperature and the quality number passes the strict rule."""
    if data_mask != 1:
        return False
    if (qa & CLEAR_FLAG) == 0 or (qa & BLOCKING_FLAGS) != 0:
        return False
    for shift in CONFIDENCE_SHIFTS:
        if (qa >> shift) & 3 >= MEDIUM_CONFIDENCE:
            return False
    return True


def cloud_evidence(qa: np.ndarray) -> np.ndarray:
    """True where a grid of quality numbers says cloud, thin cirrus, cloud shadow, or a
    medium or high confidence of any of them. The same test as is_cloud_square, for a
    whole grid at once; the caller must separately know where a number is available."""
    flags = (qa & CLOUD_RELATED_FLAGS) != 0
    for shift in CLOUD_CONFIDENCE_SHIFTS:
        flags = flags | (((qa >> shift) & 3) >= MEDIUM_CONFIDENCE)
    evidence: np.ndarray = flags
    return evidence


def is_cloud_square(qa: int, qa_valid: int) -> bool:
    """True if the quality number is available and says cloud, thin cirrus, cloud shadow
    or a medium or high confidence of any of them.

    This needs only the quality number, not a temperature: a cloud next to a gap in the
    temperature still chills the squares around it.
    """
    if qa_valid != 1:
        return False
    if (qa & CLOUD_RELATED_FLAGS) != 0:
        return True
    return any((qa >> shift) & 3 >= MEDIUM_CONFIDENCE for shift in CLOUD_CONFIDENCE_SHIFTS)


def summarize_field(
    picture: ThermalPicture, inside: list[list[bool]], field_area_m2: float
) -> ThermalSummary:
    """Count squares and work out mean, lowest and highest temperature of the clear ones.

    inside says which squares lie inside the field outline (field.inside_mask).
    field_area_m2 is the outline's real area (field.area_m2), used for real_readings: the
    grid squares are a little smaller than 30 m because they are fitted into the box, so
    their count would overstate the area. Squares outside the outline are ignored. A
    missing square INSIDE the outline counts against coverage and valid_share, so a
    partial pass cannot look complete.
    """
    if len(inside) != len(picture.kelvin) or any(
        len(inside[index]) != len(picture.kelvin[index]) for index in range(len(inside))
    ):
        raise ValueError("The outline mask and the picture are not the same size.")
    if picture.discarded_squares > 0:
        # The grid says WHERE squares were thrown out. Without it the field-only count
        # would silently read 0 and a broken pass would vanish from the report.
        if len(picture.discarded) != len(inside) or any(
            len(picture.discarded[index]) != len(inside[index]) for index in range(len(inside))
        ):
            raise ValueError("The picture has thrown-out squares but no grid saying where.")
    field_squares = 0
    observed_squares = 0
    cloud_squares = 0
    unknown_squares = 0
    discarded_field_squares = 0
    values: list[float] = []
    for row_index, row in enumerate(picture.kelvin):
        for col_index, kelvin in enumerate(row):
            qa = picture.qa[row_index][col_index]
            data_mask = picture.data_mask[row_index][col_index]
            qa_valid = picture.qa_valid[row_index][col_index]
            if qa_valid != 1:
                unknown_squares += 1  # counted in the margin too
            elif is_cloud_square(qa, qa_valid):
                cloud_squares += 1  # counted in the margin too, not only inside the outline
            if not inside[row_index][col_index]:
                continue
            field_squares += 1
            if picture.discarded:
                discarded_field_squares += picture.discarded[row_index][col_index]
            observed_squares += data_mask
            if is_clear_square(qa, data_mask):
                values.append(kelvin)
    coverage = observed_squares / field_squares if field_squares > 0 else 0.0
    valid_share = len(values) / field_squares if field_squares > 0 else 0.0
    clear_share = len(values) / observed_squares if observed_squares > 0 else 0.0
    real_readings = field_area_m2 / SENSOR_SQUARE_M**2
    if len(values) == 0:
        return ThermalSummary(
            field_squares,
            observed_squares,
            0,
            coverage,
            0.0,
            0.0,
            unknown_squares,
            cloud_squares,
            None,
            None,
            None,
            real_readings,
            discarded_field_squares,
        )
    return ThermalSummary(
        field_squares=field_squares,
        observed_squares=observed_squares,
        clear_squares=len(values),
        coverage=coverage,
        clear_share=clear_share,
        valid_share=valid_share,
        unknown_squares=unknown_squares,
        cloud_squares=cloud_squares,
        mean_k=sum(values) / len(values),
        min_k=min(values),
        max_k=max(values),
        real_readings=real_readings,
        discarded_field_squares=discarded_field_squares,
    )
