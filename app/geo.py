"""Provincias objetivo y utilidades geográficas (sin dependencias externas)."""
import json
import math
import unicodedata
from functools import lru_cache
from pathlib import Path

PROVINCES = {
    "31": "Navarra",
    "26": "La Rioja",
    "42": "Soria",
    "50": "Zaragoza",
    "22": "Huesca",
    "44": "Teruel",
    "25": "Lleida",
    "43": "Tarragona",
}

# Caja que envuelve las 8 provincias (lon_min, lat_min, lon_max, lat_max)
REGION_BBOX = (-3.2, 39.8, 1.9, 43.35)

_ALIASES = {
    "navarra": "Navarra", "nafarroa": "Navarra",
    "la rioja": "La Rioja", "rioja": "La Rioja", "rioja, la": "La Rioja",
    "soria": "Soria",
    "zaragoza": "Zaragoza",
    "huesca": "Huesca",
    "teruel": "Teruel",
    "lleida": "Lleida", "lerida": "Lleida", "lleida/lerida": "Lleida",
    "tarragona": "Tarragona",
}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def normalize_province(name: str | None) -> str | None:
    """'LERIDA', 'Lleida', 'NAVARRA' -> nombre canónico, o None si no es una de las 8."""
    if not name:
        return None
    key = _strip_accents(name).strip().lower()
    return _ALIASES.get(key)


@lru_cache(maxsize=1)
def _province_polygons():
    path = Path(__file__).parent / "data" / "provinces.geojson"
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for f in data["features"]:
        geom = f["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        out.append((f["properties"]["name"], polys))
    return out


def _in_ring(lon, lat, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def province_at(lat: float, lon: float) -> str | None:
    """Provincia (de las 8) que contiene el punto, o None."""
    for name, polys in _province_polygons():
        for poly in polys:
            if _in_ring(lon, lat, poly[0]) and not any(_in_ring(lon, lat, hole) for hole in poly[1:]):
                return name
    return None


def in_region_bbox(lat: float, lon: float) -> bool:
    lon_min, lat_min, lon_max, lat_max = REGION_BBOX
    return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max


def utm_to_latlon(x: float, y: float, zone: int = 30) -> tuple[float, float]:
    """UTM hemisferio norte (ETRS89/WGS84) -> (lat, lon) en grados."""
    a, f, k0 = 6378137.0, 1 / 298.257222101, 0.9996
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    x -= 500000.0
    m = y / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu))
    n1 = a / math.sqrt(1 - e2 * math.sin(phi1) ** 2)
    t1 = math.tan(phi1) ** 2
    c1 = ep2 * math.cos(phi1) ** 2
    r1 = a * (1 - e2) / (1 - e2 * math.sin(phi1) ** 2) ** 1.5
    d = x / (n1 * k0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (
        d ** 2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2) * d ** 6 / 720)
    lon = (d - (1 + 2 * t1 + c1) * d ** 3 / 6
           + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120) / math.cos(phi1)
    return math.degrees(lat), (zone - 1) * 6 - 180 + 3 + math.degrees(lon)


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
