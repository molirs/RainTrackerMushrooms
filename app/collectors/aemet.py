"""AEMET OpenData (requiere AEMET_API_KEY).

Cada endpoint responde primero con {"estado", "datos": <url>} y hay que descargar esa URL.
Los datos vienen en ISO-8859-15.

- Inventario de estaciones: valores/climatologicos/inventarioestaciones/todasestaciones
- Climatologías diarias (campo 'prec', mm; 'Ip' = inapreciable): van con varios días de retraso.
  Se piden en tramos de 15 días para todas las estaciones.
- Observación convencional (horaria, últimas ~24 h, campo 'prec' mm en la última hora): cubre los
  días recientes. Para tener más de 24 h hay que ejecutarla periódicamente y acumular en BD.
- Resúmenes diarios de "Últimos datos" de aemet.es (precipitación 00-24 h hora oficial, últimos 7 días):
  tapan el hueco entre la climatología y las últimas 24 h cuando la app ha estado cerrada. Son
  provisionales (se guardan como parciales) y los sustituye la climatología cuando llega. Es el mismo
  servicio que usa el mapa de la web; robots.txt pide como mucho 2 peticiones cada 10 s de día.
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests

from app import config
from app.collectors.base import (Collector, CollectorResult, HourlyObservation, MissingCredentials,
                                 Observation, PoliteSession, Station, to_float)
from app.geo import in_region_bbox, normalize_province, province_at

log = logging.getLogger(__name__)

BASE = "https://opendata.aemet.es/opendata/api"
CHUNK_DAYS = 15
DATOS_ATTEMPTS = 4
UDAT = "https://www.aemet.es/es/api-eltiempo/udat"
UDAT_INTERVAL_S = 5.0  # robots.txt de aemet.es: Request-rate 2/10 en horario diurno


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
        self.api_key = api_key if api_key is not None else config.AEMET_API_KEY
        # AEMET limita a ~50 peticiones/minuto por clave
        self.http = session or PoliteSession(min_interval=1.5)
        self.web = session or PoliteSession(min_interval=UDAT_INTERVAL_S)

    def _get(self, path: str):
        r = self.http.get(f"{BASE}/{path}", headers={"api_key": self.api_key, "Accept": "application/json"})
        r.raise_for_status()
        meta = r.json()
        if meta.get("estado") == 404:
            return []  # "No hay datos que satisfagan esos criterios"
        if meta.get("estado") != 200 or "datos" not in meta:
            raise RuntimeError(f"AEMET {path}: {meta.get('estado')} {meta.get('descripcion')}")
        # El servidor de 'datos' corta a veces la conexión a mitad de un fichero grande (visto desde el
        # móvil): los reintentos de la sesión solo cubren la conexión, no la lectura del cuerpo
        for attempt in range(DATOS_ATTEMPTS):
            try:
                d = self.http.get(meta["datos"])
                d.raise_for_status()
                return json.loads(d.content.decode("iso-8859-15"))
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError) as e:
                if attempt == DATOS_ATTEMPTS - 1:
                    raise
                log.warning("AEMET %s: %s; reintento %d", path, e, attempt + 1)

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

        # Días pasados sin climatología todavía: resumen diario provisional de la web
        have = {o.date for o in res.observations}
        today = datetime.now().date()
        missing = {start + timedelta(days=i) for i in range((end - start).days + 1)}
        missing = {d for d in missing if d < today and d not in have}
        if missing:
            try:
                self._daily_summaries(missing, stations, res)
            except Exception as e:  # noqa: BLE001
                res.errors.append(f"resumenes diarios web: {e}")

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
                st = self._station_from_point(code, o.get("lat"), o.get("lon"), o.get("ubi"), o.get("alt"))
                if st is None:
                    continue
                stations[code] = st
            # 'prec' es la lluvia de la hora que termina en fint (UTC)
            fint = datetime.strptime(o["fint"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            res.hourly.append(HourlyObservation(stations[code].station_id, self.source, fint, v))

        res.stations = list(stations.values())
        return res

    def _station_from_point(self, code: str, lat, lon, name, alt) -> Station | None:
        if lat is None or lon is None or not in_region_bbox(lat, lon):
            return None
        prov = province_at(lat, lon)
        if not prov:
            return None
        return Station(f"{self.source}:{code}", self.source, str(name or code).title(), lat, lon,
                       to_float(alt), prov)

    def _daily_summaries(self, days: set[date], stations: dict[str, Station], res: CollectorResult):
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        r = self.web.get(f"{UDAT}/timeline/diario/pre-0024", headers=headers)
        r.raise_for_status()
        files = r.json()["features"]["PB"]["DD"]["listaFicheros"]  # PB = Península y Baleares
        for f in files:
            d = datetime.strptime(f["Fecha"][:8], "%Y%m%d").date()  # "20261122T000000+0100"
            if d not in days:
                continue
            # Mismo nombre que pide el mapa de la web a zoom 9 (todas las estaciones)
            name = f["Fichero"].replace("udat_DD_PB_", "udat_DD_PB_LOCL_", 1).replace("pre_0024", "pre-0024")
            g = self.web.get(f"{UDAT}/datos/diario/pre-0024/{quote(name)}/9", headers=headers)
            g.raise_for_status()
            n = 0
            for feat in g.json().get("features", []):
                props = feat.get("properties", {})
                code, v = props.get("IDEMA"), to_float(props.get("pre-0024"))
                if not code or v is None:
                    continue
                if code not in stations:
                    lon, lat = feat["geometry"]["coordinates"][:2]
                    st = self._station_from_point(code, lat, lon, None, None)
                    if st is None:
                        continue
                    stations[code] = st
                res.observations.append(Observation(stations[code].station_id, self.source, d, v, partial=True))
                n += 1
            log.info("AEMET resumen diario web %s: %d estaciones", d, n)
