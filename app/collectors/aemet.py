"""AEMET OpenData (requiere AEMET_API_KEY).

Cada endpoint responde primero con {"estado", "datos": <url>} y hay que descargar esa URL.
Los datos vienen en ISO-8859-15.

- Inventario de estaciones: valores/climatologicos/inventarioestaciones/todasestaciones
- Climatologías diarias (campo 'prec', mm; 'Ip' = inapreciable): van con varios días de retraso.
  Se piden en tramos de 15 días para todas las estaciones.
- Observación convencional (horaria, últimas ~24 h, campo 'prec' mm en la última hora): cubre los
  días recientes. Para tener más de 24 h hay que ejecutarla periódicamente y acumular en BD.
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone

from app.collectors.base import (Collector, CollectorResult, HourlyObservation, MissingCredentials,
                                 Observation, PoliteSession, Station, to_float)
from app.config import AEMET_API_KEY
from app.geo import in_region_bbox, normalize_province, province_at

log = logging.getLogger(__name__)

BASE = "https://opendata.aemet.es/opendata/api"
CHUNK_DAYS = 15


def parse_dms(value: str) -> float:
    """'413800N' -> 41.6333 ; '010340W' -> -1.0611"""
    hemi, digits = value[-1].upper(), value[:-1]
    deg, mins, secs = int(digits[:-4]), int(digits[-4:-2]), int(digits[-2:])
    dec = deg + mins / 60 + secs / 3600
    return -dec if hemi in "SW" else dec


class AemetCollector(Collector):
    source = "aemet"
    label = "AEMET"
    attribution = "© AEMET"
    day_window = "07–07 h UTC (del día indicado al siguiente)"
    required_env = ("AEMET_API_KEY",)

    def __init__(self, api_key: str | None = None, session: PoliteSession | None = None):
        self.api_key = api_key if api_key is not None else AEMET_API_KEY
        # AEMET limita a ~50 peticiones/minuto por clave
        self.http = session or PoliteSession(min_interval=1.5)

    def _get(self, path: str):
        r = self.http.get(f"{BASE}/{path}", headers={"api_key": self.api_key, "Accept": "application/json"})
        r.raise_for_status()
        meta = r.json()
        if meta.get("estado") == 404:
            return []  # "No hay datos que satisfagan esos criterios"
        if meta.get("estado") != 200 or "datos" not in meta:
            raise RuntimeError(f"AEMET {path}: {meta.get('estado')} {meta.get('descripcion')}")
        d = self.http.get(meta["datos"])
        d.raise_for_status()
        return json.loads(d.content.decode("iso-8859-15"))

    def fetch(self, start: date, end: date) -> CollectorResult:
        if not self.api_key:
            raise MissingCredentials("Falta AEMET_API_KEY en .env")
        # Los horarios se agregan en la misma ventana 07-07 UTC que la climatología diaria de AEMET,
        # para que al llegar el diario oficial no se solapen ni se cuenten dos veces las 00-07 h
        res = CollectorResult(self.source, hourly_day_start_utc=7)

        stations: dict[str, Station] = {}
        for s in self._get("valores/climatologicos/inventarioestaciones/todasestaciones"):
            prov = normalize_province(s.get("provincia"))
            if not prov:
                continue
            code = s["indicativo"]
            stations[code] = Station(
                station_id=f"{self.source}:{code}", source=self.source, name=s["nombre"].title(),
                lat=parse_dms(s["latitud"]), lon=parse_dms(s["longitud"]),
                altitude=to_float(s.get("altitud")), province=prov,
            )

        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), end)
            path = (f"valores/climatologicos/diarios/datos/fechaini/{chunk_start}T00:00:00UTC/"
                    f"fechafin/{chunk_end}T23:59:59UTC/todasestaciones")
            try:
                rows = self._get(path)
            except Exception as e:  # noqa: BLE001
                res.errors.append(f"diarios {chunk_start}..{chunk_end}: {e}")
                rows = []
            for r in rows:
                code = r.get("indicativo")
                if code not in stations:
                    continue
                v = to_float(r.get("prec"))
                if v is None:
                    continue  # 'Acum', 'Varias' o vacío
                d = date.fromisoformat(r["fecha"])
                res.observations.append(Observation(stations[code].station_id, self.source, d, v))
            chunk_start = chunk_end + timedelta(days=1)

        # Observación convencional: últimas ~24 h en datos horarios. Se devuelven como HourlyObservation;
        # la ingesta los acumula en BD y rehace los días que aún no tienen climatología diaria.
        try:
            obs = self._get("observacion/convencional/todas")
        except Exception as e:  # noqa: BLE001
            res.errors.append(f"observacion convencional: {e}")
            obs = []
        for o in obs:
            code, v = o.get("idema"), to_float(o.get("prec"))
            if v is None or not code:
                continue
            if code not in stations:
                lat, lon = o.get("lat"), o.get("lon")
                if lat is None or lon is None or not in_region_bbox(lat, lon):
                    continue
                prov = province_at(lat, lon)
                if not prov:
                    continue
                stations[code] = Station(f"{self.source}:{code}", self.source, str(o.get("ubi", code)).title(),
                                         lat, lon, to_float(o.get("alt")), prov)
            # 'prec' es la lluvia de la hora que termina en fint (UTC)
            fint = datetime.strptime(o["fint"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            res.hourly.append(HourlyObservation(stations[code].station_id, self.source, fint, v))

        res.stations = list(stations.values())
        return res
