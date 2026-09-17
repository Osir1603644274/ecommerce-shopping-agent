from __future__ import annotations

import re
from typing import Any


BEIJING_LON_RANGE = (115.0, 118.0)
BEIJING_LAT_RANGE = (39.0, 42.0)
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _in_beijing(lon: float, lat: float) -> bool:
    return (
        BEIJING_LON_RANGE[0] <= lon <= BEIJING_LON_RANGE[1]
        and BEIJING_LAT_RANGE[0] <= lat <= BEIJING_LAT_RANGE[1]
    )


def parse_bd09_pair(value: str) -> tuple[float, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        raise ValueError(f"expected 'longitude, latitude', got {value!r}")
    lon, lat = map(float, parts)
    if not _in_beijing(lon, lat):
        raise ValueError(f"coordinate outside Beijing bounds: {value!r}")
    return lon, lat


def _plain_float(value: str) -> float | None:
    value = value.strip()
    if not _FLOAT_RE.fullmatch(value):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _compact_spaced_decimal(value: str) -> float | None:
    if not re.search(r"\d\s+\d", value):
        return None
    compact = re.sub(r"\s+", "", value)
    return _plain_float(compact)


def _parse_angle(value: str) -> tuple[float | None, str | None]:
    """Parse common decimal/DMS exports without guessing arbitrary bad strings."""
    raw = value.strip()
    if not raw or raw in {"/", "-", "无", "暂无"}:
        return None, None

    plain = _plain_float(raw)
    if plain is not None:
        return plain, "decimal"

    spaced = _compact_spaced_decimal(raw)
    if spaced is not None:
        return spaced, "spaced_decimal"

    numbers = [float(item) for item in _NUMBER_RE.findall(raw)]
    has_angle_marker = bool(
        re.search(r"[°º度′'＇分″\"＂〃秒]|东经|北纬|[EN]", raw, re.IGNORECASE)
    )
    if not has_angle_marker or not numbers:
        return None, None

    if len(numbers) >= 3:
        degree, minute, second = numbers[:3]
        if minute >= 60 or second >= 60:
            return None, None
        return degree + minute / 60 + second / 3600, "dms"

    if len(numbers) == 2:
        degree, suffix = numbers
        # Some source rows contain forms such as 116°6950810. They appear to
        # encode a decimal suffix, but the precision is not authoritative.
        suffix_text = _NUMBER_RE.findall(raw)[1]
        if suffix >= 60 and "." not in suffix_text:
            return float(f"{int(degree)}.{suffix_text}"), "decimal_suffix"
        if suffix < 60:
            return degree + suffix / 60, "degree_minute"
        return None, None

    return numbers[0], "marked_decimal"


def parse_toilet_coordinate(longitude_raw: str, latitude_raw: str) -> dict[str, Any]:
    lon_value, lon_method = _parse_angle(longitude_raw)
    lat_value, lat_method = _parse_angle(latitude_raw)
    result: dict[str, Any] = {
        "longitude": None,
        "latitude": None,
        "coordinate_quality": "invalid",
        "coordinate_parse_method": None,
    }
    if lon_value is None or lat_value is None:
        return result

    reversed_pair = False
    if _in_beijing(lon_value, lat_value):
        lon, lat = lon_value, lat_value
    elif _in_beijing(lat_value, lon_value):
        lon, lat = lat_value, lon_value
        reversed_pair = True
    else:
        result["coordinate_quality"] = "out_of_bounds"
        return result

    methods = {lon_method, lat_method}
    if reversed_pair and methods == {"decimal"}:
        quality = "corrected_reversed_decimal"
        method = "reversed_decimal"
    elif methods == {"decimal"}:
        quality = "high"
        method = "decimal"
    elif methods <= {"decimal", "spaced_decimal"}:
        quality = "high"
        method = "spaced_decimal"
    elif "decimal_suffix" in methods:
        quality = "low"
        method = "decimal_suffix"
    elif "degree_minute" in methods or "marked_decimal" in methods:
        quality = "low"
        method = "degree_minute"
    elif methods <= {"decimal", "dms"}:
        quality = "high"
        method = "dms"
    else:
        quality = "low"
        method = "+".join(sorted(str(item) for item in methods))

    result.update(
        longitude=round(lon, 8),
        latitude=round(lat, 8),
        coordinate_quality=quality,
        coordinate_parse_method=method,
    )
    return result
