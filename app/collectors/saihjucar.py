"""SAIH Júcar (Confederación Hidrográfica del Júcar): pluviómetros de Castellón y del sur de Teruel.

Sin clave. Es el endpoint público que usa su propio mapa de lluvias (saih.chj.es/mapa-lluvias):
- GET /lluviasIntervalo/AAAA-MM-DD/AAAA-MM-DD devuelve, para cada pluviómetro, la lluvia acumulada en el
  intervalo ('lluvia_int', mm) y cuántos días tienen dato ('valores'). Pidiendo un único día se obtiene
  el total diario 00–24 h hora local (coincide con su 'lluvia_24h' a las 23:55).
- El día en curso no se publica (devuelve []) y el de ayer se va completando a lo largo del día, así que
  solo se cachean en disco los días cerrados.
- Coordenadas en UTM 30N ETRS89, con los nombres cambiados: X en 'fldNCoordGPSLat' e Y en 'fldNCoordGPSLon'.
- No da altitud: se pide una vez a la API de elevación de Open-Meteo (DEM de 90 m) y se cachea.
"""
import json
import logging
from datetime import date, timedelta

from app.collectors.base import Collector, CollectorResult, Observation, PoliteSession, Station, to_float
from app.config import CACHE_DIR
from app.geo import province_at, utm_to_latlon

log = logging.getLogger(__name__)

BASE = "https://saih.chj.es"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
CACHE = CACHE_DIR / "saihjucar"


def parse_station(row: dict, source: str) -> Station | None:
    """Fila de /lluviasIntervalo -> Station, o None si está fuera de las provincias objetivo."""
    code = (row.get("fldTCodigo") or "").strip()
    x, y = to_float(row.get("fldNCoordGPSLat")), to_float(row.get("fldNCoordGPSLon"))
    if not code or not x or not y:
        return None
    lat, lon = utm_to_latlon(x, y, zone=30)
    prov = province_at(lat, lon)
    if not prov:  # la cuenca es sobre todo Valencia, Cuenca, Albacete y Alicante
        return None
    return Station(station_id=f"{source}:{code}", source=source, name=row.get("fldTNombre", code).strip().title(),
                   lat=round(lat, 5), lon=round(lon, 5), altitude=None, province=prov)


class SaihJucarCollector(Collector):
    source = "saihjucar"
    label = "SAIH Júcar (CHJ)"
    attribution = "© Confederación Hidrográfica del Júcar — SAIH Júcar (datos provisionales)"
    day_window = "00–24 h (hora local)"

    def __init__(self, session: PoliteSession | None = None):
        self.http = session or PoliteSession(min_interval=2.0)

    def _fetch_day(self, day: date) -> list[dict]:
        r = self.http.get(f"{BASE}/lluviasIntervalo/{day.isoformat()}/{day.isoformat()}")
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise RuntimeError(f"SAIH Júcar {day}: {str(data)[:200]}")
        return data

    def _day(self, day: date, today: date) -> list[dict]:
        """Filas de un día, de la caché si ya está cerrado."""
        path = CACHE / "daily" / f"{day.isoformat()}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        rows = self._fetch_day(day)
        if day <= today - timedelta(days=2) and rows:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return rows

    def _altitudes(self, stations: list[Station], res: CollectorResult) -> None:
        path = CACHE / "elevation.json"
        cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        missing = [s for s in stations if s.station_id not in cache]
        for i in range(0, len(missing), 100):
            chunk = missing[i:i + 100]
            try:
                r = self.http.get(ELEVATION_URL, params={"latitude": ",".join(str(s.lat) for s in chunk),
                                                         "longitude": ",".join(str(s.lon) for s in chunk)})
                r.raise_for_status()
                cache.update(zip((s.station_id for s in chunk), r.json()["elevation"]))
            except Exception as e:  # noqa: BLE001 - sin altitud la estación sigue valiendo
                res.errors.append(f"altitudes (Open-Meteo): {e}")
                break
        if missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cache), encoding="utf-8")
        for s in stations:
            s.altitude = to_float(cache.get(s.station_id))

    def fetch(self, start: date, end: date) -> CollectorResult:
        res = CollectorResult(self.source)
        today = date.today()
        stations: dict[str, Station] = {}
        day = min(end, today - timedelta(days=1))  # hoy siempre viene vacío
        while day >= start:
            try:
                rows = self._day(day, today)
            except Exception as e:  # noqa: BLE001 - un día fallido no tumba el resto
                res.errors.append(f"{day}: {e}")
                log.warning("SAIH Júcar %s: %s", day, e)
                if len(res.errors) >= 3 and not res.observations:
                    raise RuntimeError("; ".join(res.errors)) from None
                day -= timedelta(days=1)
                continue
            for row in rows:
                mm = to_float(row.get("lluvia_int"))
                if mm is None or mm < 0 or not row.get("valores"):
                    continue
                code = (row.get("fldTCodigo") or "").strip()
                st = stations.get(code)
                if st is None:
                    st = parse_station(row, self.source)
                    if st is None:
                        continue
                    stations[code] = st
                res.observations.append(Observation(st.station_id, self.source, day, round(mm, 1)))
            day -= timedelta(days=1)

        res.stations = list(stations.values())
        self._altitudes(res.stations, res)
        return res
