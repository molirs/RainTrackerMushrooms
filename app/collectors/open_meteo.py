"""Open-Meteo: lluvia ESTIMADA (modelo) en una rejilla que cubre las 8 provincias.

No son medidas: sirve para rellenar zonas de monte sin estaciones. Las estaciones generadas
llevan estimated=True para que el frontend las pinte distinto.
Uso gratuito solo no comercial; límite ~10.000 llamadas/día (cada punto cuenta como una).
"""
import logging
from datetime import date

from app.collectors.base import Collector, CollectorResult, Observation, PoliteSession, Station
from app.geo import REGION_BBOX, province_at

log = logging.getLogger(__name__)

URL = "https://api.open-meteo.com/v1/forecast"
BATCH = 100  # puntos por petición


def grid_points(step: float) -> list[tuple[float, float, str]]:
    lon_min, lat_min, lon_max, lat_max = REGION_BBOX
    pts = []
    lat = lat_min
    while lat <= lat_max:
        lon = lon_min
        while lon <= lon_max:
            prov = province_at(lat, lon)
            if prov:
                pts.append((round(lat, 3), round(lon, 3), prov))
            lon += step
        lat += step
    return pts


class OpenMeteoCollector(Collector):
    source = "openmeteo"
    label = "Open-Meteo (estimado)"
    attribution = "Estimación de modelo: Open-Meteo.com (CC BY 4.0)"
    day_window = "00–24 h hora peninsular"

    def __init__(self, step: float = 0.15, session: PoliteSession | None = None):
        self.step = step
        self.http = session or PoliteSession(min_interval=1.0)

    def fetch(self, start: date, end: date) -> CollectorResult:
        res = CollectorResult(self.source)
        pts = grid_points(self.step)
        today = date.today()
        for i in range(0, len(pts), BATCH):
            chunk = pts[i:i + BATCH]
            params = {
                "latitude": ",".join(str(p[0]) for p in chunk),
                "longitude": ",".join(str(p[1]) for p in chunk),
                "daily": "precipitation_sum",
                "timezone": "Europe/Madrid",
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            }
            try:
                r = self.http.get(URL, params=params)
                r.raise_for_status()
            except Exception as e:  # noqa: BLE001 - un lote fallido no debe tumbar el resto
                res.errors.append(f"lote {i // BATCH}: {e}")
                continue
            data = r.json()
            if isinstance(data, dict):
                data = [data]
            for (lat, lon, prov), loc in zip(chunk, data):
                sid = f"{self.source}:{lat:.3f},{lon:.3f}"
                res.stations.append(Station(
                    station_id=sid, source=self.source, name=f"Rejilla {lat:.2f}, {lon:.2f}",
                    lat=lat, lon=lon, altitude=loc.get("elevation"), province=prov, estimated=True,
                ))
                daily = loc.get("daily", {})
                for d, v in zip(daily.get("time", []), daily.get("precipitation_sum", [])):
                    if v is None:
                        continue
                    day = date.fromisoformat(d)
                    res.observations.append(Observation(sid, self.source, day, float(v), partial=day >= today))
        return res
