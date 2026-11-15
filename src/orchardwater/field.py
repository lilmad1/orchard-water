"""Read a field outline from a GeoJSON file.

GeoJSON is a plain-text (JSON) file format for shapes on a map. You can draw a
field on geojson.io and save it. Coordinates are written [longitude, latitude],
in that order (x first, then y), which is the opposite of how people usually
say "lat, lon". Getting this order wrong puts the field in the wrong country.

This module only reads and checks the outline. It does not talk to the internet.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Mean radius of the Earth, IUGG value. Used to turn degrees into metres.
EARTH_RADIUS_M = 6_371_000.0

# One degree of latitude in metres. The Earth is a sphere here, which is off by
# well under 1 % at 30 degrees north - fine for sizing a picture request.
METRES_PER_DEGREE_LAT = EARTH_RADIUS_M * math.pi / 180.0

# A safety limit on how big a field outline may be, so a typing mistake (for
# example a corner at longitude 90 instead of 9.0) cannot ask the satellite
# service for a picture the size of half a country.
MAX_SIDE_M = 20_000.0


@dataclass(frozen=True)
class Field:
    """One field outline.

    corners: the corner points as (longitude, latitude) in degrees. The first
        point is NOT repeated at the end here.
    west, south, east, north: the smallest box around the field, in degrees.
    width_m, height_m: the size of that box in metres (east-west, north-south).
    """

    corners: list[tuple[float, float]]
    west: float
    south: float
    east: float
    north: float
    width_m: float
    height_m: float


def load_field(path: Path) -> Field:
    """Read a GeoJSON file and return the one field polygon inside it.

    Raises ValueError with a plain message if the file is not a single simple
    polygon in longitude/latitude degrees.
    """
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except OSError as error:
        raise ValueError(f"Could not read field file {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Field file {path} is not valid JSON: {error}") from error
    geometry = _find_polygon_geometry(data)
    ring = _outer_ring(geometry)
    corners = _check_corners(ring)
    _check_simple_shape(corners)
    return _build_field(corners)


def inside_mask(field: Field, width: int, height: int) -> list[list[bool]]:
    """Say which squares of a width x height picture lie inside the outline.

    The picture covers the field's box: row 0 is the north edge and column 0
    the west edge, which is how earth_search.py builds it. A square counts as
    inside when its centre point is inside the outline.
    """
    mask: list[list[bool]] = []
    for row in range(height):
        lat = field.north - (row + 0.5) / height * (field.north - field.south)
        line: list[bool] = []
        for col in range(width):
            lon = field.west + (col + 0.5) / width * (field.east - field.west)
            line.append(_point_in_polygon(lon, lat, field.corners))
        mask.append(line)
    return mask


def _point_in_polygon(lon: float, lat: float, corners: list[tuple[float, float]]) -> bool:
    """Ray casting: go east from the point and count the outline edges crossed.

    An odd count means the point is inside. Plain degrees are fine here: a
    field is tiny, so the flat-map error is far smaller than one square.
    """
    inside = False
    count = len(corners)
    for index in range(count):
        x1, y1 = corners[index]
        x2, y2 = corners[(index + 1) % count]
        if (y1 > lat) != (y2 > lat):  # this edge spans the point's latitude
            crossing_lon = x1 + (lat - y1) / (y2 - y1) * (x2 - x1)
            if lon < crossing_lon:
                inside = not inside
    return inside


def _find_polygon_geometry(data: Any) -> dict[str, Any]:
    """Dig the polygon out of a GeoJSON document.

    geojson.io saves a FeatureCollection (a list of shapes). We accept that
    only when it holds exactly one shape, plus the two simpler layouts.
    """
    if not isinstance(data, dict):
        raise ValueError("Field file must contain a JSON object.")
    kind = data.get("type")
    if kind == "FeatureCollection":
        features = data.get("features")
        if not isinstance(features, list) or len(features) != 1:
            raise ValueError("Field file must contain exactly one shape (one Feature).")
        return _find_polygon_geometry(features[0])
    if kind == "Feature":
        geometry = data.get("geometry")
        if not isinstance(geometry, dict):
            raise ValueError("The Feature in the field file has no geometry.")
        return _find_polygon_geometry(geometry)
    if kind == "Polygon":
        return data
    raise ValueError(f"Field shape must be a Polygon, not {kind!r}.")


def _outer_ring(geometry: dict[str, Any]) -> list[Any]:
    """Return the outline of the polygon; refuse polygons with holes."""
    rings = geometry.get("coordinates")
    if not isinstance(rings, list) or len(rings) == 0:
        raise ValueError("The polygon has no coordinates.")
    if len(rings) != 1:
        raise ValueError("Polygons with holes are not supported yet.")
    ring = rings[0]
    if not isinstance(ring, list):
        raise ValueError("The polygon outline must be a list of points.")
    return ring


def _check_corners(ring: list[Any]) -> list[tuple[float, float]]:
    """Check every point and return the corners without the repeated last point.

    GeoJSON closes a shape by repeating the first point at the end, so a
    triangle is written with 4 points. We need at least 3 different corners.
    """
    if len(ring) < 4:
        raise ValueError("A polygon needs at least 3 corners (4 points, first repeated).")
    if ring[0] != ring[-1]:
        raise ValueError("The polygon is not closed: last point must equal the first.")
    corners: list[tuple[float, float]] = []
    for point in ring[:-1]:
        corners.append(_check_point(point))
    return corners


def _check_point(point: Any) -> tuple[float, float]:
    """Check one [longitude, latitude] pair and return it as a tuple."""
    if not isinstance(point, list) or len(point) < 2:
        raise ValueError(f"A point must be [longitude, latitude], got {point!r}.")
    lon, lat = point[0], point[1]
    for value in (lon, lat):
        # bool is a kind of int in Python, and true/false is not a coordinate.
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"Coordinates must be numbers, got {point!r}.")
        if not math.isfinite(value):
            raise ValueError(f"Coordinates must be finite numbers, got {point!r}.")
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"Longitude {lon} is outside -180 to 180.")
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"Latitude {lat} is outside -90 to 90.")
    return (float(lon), float(lat))


def _check_simple_shape(corners: list[tuple[float, float]]) -> None:
    """Refuse an outline that is not one ordinary shape with an area.

    It needs at least 3 different corners, no edge may cross or touch another edge
    it does not share a corner with (a bow-tie or figure of 8 would give an unclear
    footprint), and the shape must have a real area.
    """
    if len(set(corners)) < 3:
        raise ValueError("The outline needs at least 3 different corners.")
    count = len(corners)
    for first in range(count):
        for second in range(first + 1, count):
            shares_a_corner = second == first + 1 or (first == 0 and second == count - 1)
            if shares_a_corner:
                continue
            a1, a2 = corners[first], corners[(first + 1) % count]
            b1, b2 = corners[second], corners[(second + 1) % count]
            if _edges_touch(a1, a2, b1, b2):
                raise ValueError(
                    "The outline crosses or touches itself; redraw it as one simple shape."
                )
    # The crossing check comes first: a bow-tie's two halves cancel out in the area
    # sum, so a bow-tie could otherwise be reported as "no area".
    lons = [lon for lon, _ in corners]
    lats = [lat for _, lat in corners]
    box_area = (max(lons) - min(lons)) * (max(lats) - min(lats))
    # Compared with the box, so rounding noise on a flat line is not mistaken for area.
    if box_area <= 0.0 or _shape_area(corners) <= 1e-9 * box_area:
        raise ValueError("The polygon has no area (all points in a line).")


def area_m2(field: Field) -> float:
    """The area inside the field outline, in square metres.

    The outline's area in square degrees (shoelace formula) is turned into square
    metres by multiplying with the metres in one degree of latitude and in one degree
    of longitude (which shrinks with cos(latitude), at the middle of the box).
    Hand check: a 100 m x 100 m square outline gives 10,000 m2.
    """
    middle_lat = (field.south + field.north) / 2.0
    metres_per_degree_lon = METRES_PER_DEGREE_LAT * math.cos(math.radians(middle_lat))
    return _shape_area(field.corners) * METRES_PER_DEGREE_LAT * metres_per_degree_lon


def with_margin(field: Field, margin_m: float) -> Field:
    """The same outline in a bigger box: margin_m metres of extra map on every side.

    Only the box (west, south, east, north and its size in metres) grows. The corners,
    and so the outline and its area, stay the same, which means inside_mask() on the
    bigger box still marks only the squares that lie inside the real outline.
    """
    middle_lat = (field.south + field.north) / 2.0
    metres_per_degree_lon = METRES_PER_DEGREE_LAT * math.cos(math.radians(middle_lat))
    margin_lat = margin_m / METRES_PER_DEGREE_LAT
    margin_lon = margin_m / metres_per_degree_lon
    return Field(
        corners=field.corners,
        west=field.west - margin_lon,
        south=field.south - margin_lat,
        east=field.east + margin_lon,
        north=field.north + margin_lat,
        width_m=field.width_m + 2.0 * margin_m,
        height_m=field.height_m + 2.0 * margin_m,
    )


def _shape_area(corners: list[tuple[float, float]]) -> float:
    """Area of the shape in square degrees (the shoelace formula)."""
    total = 0.0
    for index in range(len(corners)):
        x1, y1 = corners[index]
        x2, y2 = corners[(index + 1) % len(corners)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _turn(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> int:
    """Which way you turn going p to q to r: 1 left, -1 right, 0 in a straight line."""
    value = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return (value > 0) - (value < 0)


def _within(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> bool:
    """For r on the line through p and q: is r between p and q?"""
    return min(p[0], q[0]) <= r[0] <= max(p[0], q[0]) and min(p[1], q[1]) <= r[1] <= max(p[1], q[1])


def _edges_touch(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    """True if the edge a1-a2 and the edge b1-b2 cross, touch or overlap."""
    t1, t2 = _turn(a1, a2, b1), _turn(a1, a2, b2)
    t3, t4 = _turn(b1, b2, a1), _turn(b1, b2, a2)
    if t1 != t2 and t3 != t4:
        return True  # each edge's ends lie on opposite sides of the other: they cross
    if t1 == 0 and _within(a1, a2, b1):
        return True
    if t2 == 0 and _within(a1, a2, b2):
        return True
    if t3 == 0 and _within(b1, b2, a1):
        return True
    return t4 == 0 and _within(b1, b2, a2)


def _build_field(corners: list[tuple[float, float]]) -> Field:
    """Work out the box around the corners and its size in metres."""
    lons = [lon for lon, _ in corners]
    lats = [lat for _, lat in corners]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    # A degree of longitude gets shorter towards the poles: multiply by
    # cos(latitude). Use the middle latitude of the box.
    middle_lat = (south + north) / 2.0
    metres_per_degree_lon = METRES_PER_DEGREE_LAT * math.cos(math.radians(middle_lat))
    width_m = (east - west) * metres_per_degree_lon
    height_m = (north - south) * METRES_PER_DEGREE_LAT
    if width_m <= 0.0 or height_m <= 0.0:
        raise ValueError("The polygon has no area (all points in a line).")
    if width_m > MAX_SIDE_M or height_m > MAX_SIDE_M:
        raise ValueError(
            f"The field box is {width_m:.0f} m x {height_m:.0f} m; "
            f"the limit is {MAX_SIDE_M:.0f} m per side. Check the coordinates."
        )
    return Field(
        corners=corners,
        west=west,
        south=south,
        east=east,
        north=north,
        width_m=width_m,
        height_m=height_m,
    )
