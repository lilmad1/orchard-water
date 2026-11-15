"""Turn a Sentinel-2 picture into greenness (NDVI) numbers.

NDVI is a number from -1 to 1 that says how green a spot is. Plants reflect a
lot of near-infrared light and little red light, so:
    NDVI = (near-infrared - red) / (near-infrared + red)
Dense healthy leaves score high (about 0.6 to 0.9), bare soil scores low
(about 0.1 to 0.2). This is greenness only. It is NOT a measure of water stress.

This module does no internet work. earth_search.py fetches the picture; this
file checks it and does the math. Grids are plain lists of rows, so the math
below stays in simple loops.
"""

import math
from dataclasses import dataclass

import numpy as np

# Scene classes (SCL) we trust for greenness: 4 = vegetation, 5 = bare soil.
# Everything else is dropped: 0 no data, 1 defective, 2 dark area, 3 cloud
# shadow, 6 water, 7 low-probability cloud or unclassified, 8 and 9 cloud,
# 10 thin cirrus, 11 snow.
# Chosen "strict" on 2026-09-30 after a real September check near Agadir
# showed only classes 4, 5, 8, 9 and 10 (see docs/sentinel2-reference.md).
# TODO(verify): class list is from the Copernicus docs (2026-09-30); cross-check
# with the ESA Sen2Cor documentation.
CLEAR_SCL_CLASSES = frozenset({4, 5})

# The scene labels the Sentinel-2 L2A product uses run from 0 to 11.
HIGHEST_SCL_CLASS = 11


@dataclass(frozen=True)
class Picture:
    """One day's picture as four grids of the same size (rows of squares).

    red, nir: reflectance, the share of sunlight the ground sends back (0 is none,
        1 is all; a bright surface can read slightly above 1, but never below 0).
    scl: scene class number for each square (see CLEAR_SCL_CLASSES).
    data_mask: 1 where the satellite had data for the square, 0 where not.
    discarded_squares: photographed squares left out because their red or
        near-infrared value was missing or below 0 (they show as data_mask 0).
    pictures_combined: how many satellite pictures of the same day gave at least
        one square (1 for a normal day; 2 when the two Sentinel-2 satellites both
        passed and the day was put together from both).
    picture_ids: the catalogue IDs of the day's pictures, newest first.
    source_picture: for each square, the position in picture_ids of the picture its
        reading came from (0 = newest), or -1 where no picture had data. None when the
        Picture was not built from catalogue pictures (for example in tests).
    replaced_unclear: True for a square where an older picture replaced a reading that
        was not clear in a newer one. None when source_picture is None.
    """

    red: list[list[float]]
    nir: list[list[float]]
    scl: list[list[int]]
    data_mask: list[list[int]]
    discarded_squares: int = 0
    pictures_combined: int = 1
    picture_ids: tuple[str, ...] = ()
    source_picture: list[list[int]] | None = None
    replaced_unclear: list[list[bool]] | None = None


@dataclass(frozen=True)
class FieldSummary:
    """Greenness of one field on one day.

    field_squares: squares whose centre lies inside the field outline.
    observed_squares: field squares where the satellite had data.
    clear_squares: observed squares that pass the cloud rule and have an NDVI.
    coverage: observed_squares / field_squares (0 to 1). Below 1 means the
        satellite only photographed part of the field that day.
    clear_share: clear_squares / observed_squares (0 to 1), or 0.0 if none
        were observed. It only speaks for the observed part: read it together
        with coverage.
    mean_ndvi, min_ndvi, max_ndvi: over the clear squares, or None if none.
    """

    field_squares: int
    observed_squares: int
    clear_squares: int
    coverage: float
    clear_share: float
    mean_ndvi: float | None
    min_ndvi: float | None
    max_ndvi: float | None


def picture_from_bands(
    red: np.ndarray, nir: np.ndarray, scl: np.ndarray, photographed: np.ndarray
) -> Picture:
    """Build a checked Picture from four same-size grids (rows x columns).

    red, nir: reflectance (0 to about 1); NaN or below 0 means the value is unusable.
    scl: scene class number per square (0 to 11).
    photographed: True where the satellite had data for the square.
    A photographed square with an unusable red or nir value is left out of the
    Picture (data_mask 0) and counted in discarded_squares, so it can be reported.
    """
    with np.errstate(invalid="ignore"):  # comparing a missing value is fine here
        label_ok = (
            np.isfinite(scl) & (scl == np.rint(scl)) & (scl >= 0) & (scl <= HIGHEST_SCL_CLASS)
        )
        light_ok = np.isfinite(red) & np.isfinite(nir) & (red >= 0) & (nir >= 0)
    if (photographed & ~label_ok).any():
        raise ValueError("The scene label layer holds values that are not whole numbers 0 to 11.")
    usable = photographed & light_ok
    discarded = int((photographed & ~light_ok).sum())
    return Picture(
        red=np.where(light_ok, red, 0.0).astype(float).tolist(),
        nir=np.where(light_ok, nir, 0.0).astype(float).tolist(),
        scl=np.where(label_ok, np.rint(scl), 0).astype(int).tolist(),
        data_mask=usable.astype(int).tolist(),
        discarded_squares=discarded,
    )


def ndvi(red: float, nir: float) -> float | None:
    """NDVI for one square from its red and near-infrared reflectance.

    Returns None when there is no reliable answer: a value is not a finite
    number, or red + nir is not above zero (the formula divides by it).
    """
    if not math.isfinite(red) or not math.isfinite(nir):
        return None
    total = nir + red
    if total <= 0.0:
        return None
    return (nir - red) / total


def is_clear_square(scl: int, data_mask: int) -> bool:
    """True if the satellite had data here and the scene class is one we trust."""
    return data_mask == 1 and scl in CLEAR_SCL_CLASSES


def square_source(picture: Picture, row: int, col: int) -> tuple[str | None, str]:
    """Which picture one square came from, and why: (picture id or None, reason in words).

    The reason is one of four plain sentences. It needs a Picture built by
    earth_search.picture_for_day; for any other Picture it raises ValueError.
    """
    if picture.source_picture is None or picture.replaced_unclear is None:
        raise ValueError("This Picture does not record where each square came from.")
    position = picture.source_picture[row][col]
    if position < 0:
        return None, "no picture had data for this square"
    picture_id = picture.picture_ids[position]
    if position == 0:
        return picture_id, "the newest picture of the day"
    if picture.replaced_unclear[row][col]:
        return picture_id, "an older picture, because the newer one was not clear here"
    return picture_id, "an older picture, because the newer one had no data here"


def ndvi_grid(picture: Picture) -> list[list[float | None]]:
    """NDVI for every square, or None where the square is not clear."""
    grid: list[list[float | None]] = []
    for row_index in range(len(picture.red)):
        row: list[float | None] = []
        for col_index in range(len(picture.red[row_index])):
            if is_clear_square(
                picture.scl[row_index][col_index], picture.data_mask[row_index][col_index]
            ):
                row.append(
                    ndvi(picture.red[row_index][col_index], picture.nir[row_index][col_index])
                )
            else:
                row.append(None)
        grid.append(row)
    return grid


def summarize_field(picture: Picture, inside: list[list[bool]]) -> FieldSummary:
    """Count squares and work out mean, lowest and highest NDVI of the clear ones.

    inside says which squares lie inside the field outline (field.inside_mask).
    Squares outside it are ignored completely. A missing square INSIDE the
    outline counts against coverage, so a partial satellite pass cannot look
    like a complete one.
    """
    if len(inside) != len(picture.red) or any(
        len(inside[index]) != len(picture.red[index]) for index in range(len(inside))
    ):
        raise ValueError("The outline mask and the picture are not the same size.")
    grid = ndvi_grid(picture)
    field_squares = 0
    observed_squares = 0
    values: list[float] = []
    for row_index in range(len(grid)):
        for col_index in range(len(grid[row_index])):
            if not inside[row_index][col_index]:
                continue
            field_squares += 1
            if picture.data_mask[row_index][col_index] == 1:
                observed_squares += 1
            value = grid[row_index][col_index]
            if value is not None:
                values.append(value)
    coverage = observed_squares / field_squares if field_squares > 0 else 0.0
    if len(values) == 0:
        return FieldSummary(field_squares, observed_squares, 0, coverage, 0.0, None, None, None)
    return FieldSummary(
        field_squares=field_squares,
        observed_squares=observed_squares,
        clear_squares=len(values),
        coverage=coverage,
        clear_share=len(values) / observed_squares,
        mean_ndvi=sum(values) / len(values),
        min_ndvi=min(values),
        max_ndvi=max(values),
    )
