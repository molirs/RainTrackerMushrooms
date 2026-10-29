"""SAIH Ebro (Confederación Hidrográfica del Ebro) vía su API Open Data (requiere SAIH_API_KEY).

- Lluvia: GET /datos/apiopendata?tipo_senal=PACUM&inicio=YYYY-MM-DD devuelve, para todos los
  pluviómetros (~330), las lecturas quinceminutales del acumulado del día (00-24 h, hora local) desde
  D 00:00 hasta D+1 00:00. La lectura de D 00:00 es el total de D-1 y la de D+1 00:00 el total de D.
  Sin 'inicio' devuelve solo el dato actual. Hay datos de los últimos 365 días.
- Límite: 5 llamadas por minuto por clave. Cada día son ~5 MB, así que los días cerrados se guardan
  en caché y solo se vuelven a pedir ayer y hoy.
- Estaciones (coordenadas y altitud): la API no las da; se toman de la capa pública SAIH_Precipitacion
  del geoportal SITEbro de la CHE (ArcGIS). El código de estación son los 4 primeros caracteres de la
  señal (A005E84PACUM -> A005).
"""
import json
import logging
import statistics
import time
from datetime import date, datetime, timedelta

import requests

from app import config
from app.collectors.base import (Collector, CollectorResult, MissingCredentials, Observation, PoliteSession,
                                 Station, to_float)
from app.config import CACHE_DIR
from app.geo import province_at

log = logging.getLogger(__name__)

API_URL = "https://www.saihebro.com/datos/apiopendata"
STATIONS_URL = ("https://sitebro-srv.chebro.es/server/rest/services/Sitebro_Internet/Inventario/"
                "MapServer/93/query")
CACHE = CACHE_DIR / "saihebro"
STATIONS_MAX_AGE_S = 7 * 86400
MAX_DAYS_BACK = 365
QC_VERSION = 2       # cambiar si cambia el filtro, para rehacer la caché
DRIP_WINDOW = 12     # cuartos de hora seguidos con lluvia (3 h)...
DRIP_MIN_MM = 1.0    # ...de al menos 1 mm de media cada uno...
DRIP_MAX_CV = 0.12   # ...y casi idénticos


def is_drip(increments: list[float]) -> bool:
    """Pluviómetro averiado que 'gotea': suma casi lo mismo cada 15 min durante horas (visto en
    Cadrete: +1,9 mm cada cuarto de hora todo un día, o +5,6..6,6 durante 5 h, con 0 mm alrededor).
    La lluvia real es irregular: en un día de lluvia generalizada ninguna ventana de 3 h baja de
    un coef. de variación de 0,24, y eso con llovizna de 0,2 mm (que el mínimo de mm excluye)."""
    w = DRIP_WINDOW
    for i in range(len(increments) - w + 1):
        x = increments[i:i + w]
        if min(x) <= 0:
            continue
        mean = statistics.mean(x)
        if mean >= DRIP_MIN_MM and statistics.pstdev(x) / mean < DRIP_MAX_CV:
            return True
    return False


def daily_totals(readings: list[dict], day: date) -> tuple[dict[str, float], set[str], set[str]]:
    """Total de lluvia de 'day' por estación a partir de las lecturas de 'inicio=day'.
    Devuelve (mm por código, códigos cuyo día está completo, códigos descartados por avería).
    Completo = hay lectura de D+1 00:00."""
    next_midnight = f"{(day + timedelta(days=1)).isoformat()} 00:00:00"
    day_prefix = day.isoformat()
    series: dict[str, list[tuple[str, float]]] = {}
    for r in readings:
        v = to_float(r.get("valor"))
        fecha = r.get("fecha") or ""
        if v is None or v < 0:
            continue
        # D 00:00 es el cierre de D-1
        if fecha == next_midnight or (fecha.startswith(day_prefix) and not fecha.endswith(" 00:00:00")):
            series.setdefault(r["senal"][:4], []).append((fecha, v))
    totals: dict[str, float] = {}
    complete: set[str] = set()
    suspect: set[str] = set()
    for code, pts in series.items():
        pts.sort()
        values = [v for _, v in pts]
        if is_drip([b - a for a, b in zip(values, values[1:])]):
            suspect.add(code)
            continue
        if pts[-1][0] == next_midnight:
            totals[code] = values[-1]
            complete.add(code)
        else:  # el acumulado solo crece a lo largo del día
            totals[code] = max(values)
    return {c: round(v, 1) for c, v in totals.items()}, complete, suspect


class SaihEbroCollector(Collector):
    source = "saihebro"
    label = "SAIH Ebro (CHE)"
    attribution = "© Confederación Hidrográfica del Ebro — SAIH Ebro (datos provisionales)"
    day_window = "00–24 h (hora local)"
    required_env = ("SAIH_API_KEY",)

    def __init__(self, api_key: str | None = None, session: PoliteSession | None = None):
        self.api_key = api_key if api_key is not None else config.SAIH_API_KEY
        # 5 llamadas/minuto: una cada 13 s deja margen
        self.http = session or PoliteSession(min_interval=13, retries=2)
        self.geo = PoliteSession(min_interval=1.0)

    # ---- Estaciones ----------------------------------------------------------------------------
    def _stations(self) -> dict[str, Station]:
        path = CACHE / "stations.json"
        features = None
        if path.exists() and time.time() - path.stat().st_mtime < STATIONS_MAX_AGE_S:
            features = json.loads(path.read_text(encoding="utf-8"))
        else:
            try:
                r = self.geo.get(STATIONS_URL, params={
                    "where": "1=1", "outFields": "LRS_CODIGO_TXT,LRS_NOMBRE,LRS_UTM_Z",
                    "returnGeometry": "true", "outSR": "4326", "f": "json"})
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    raise RuntimeError(f"SITEbro: {data['error'].get('message')}")
                features = data["features"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(features, ensure_ascii=False), encoding="utf-8")
            except Exception:
                if not path.exists():
                    raise
                log.warning("SITEbro no responde; uso la lista de estaciones en caché", exc_info=True)
                features = json.loads(path.read_text(encoding="utf-8"))

        out = {}
        for f in features:
            a, g = f["attributes"], f.get("geometry") or {}
            code = (a.get("LRS_CODIGO_TXT") or "").strip()
            lat, lon = g.get("y"), g.get("x")
            if not code or lat is None or lon is None:
                continue
            prov = province_at(lat, lon)
            if not prov:  # la cuenca llega a Cantabria, Burgos, Álava, Castellón, Andorra...
                continue
            out[code] = Station(
                station_id=f"{self.source}:{code}", source=self.source,
                name=(a.get("LRS_NOMBRE") or code).strip(), lat=lat, lon=lon,
                altitude=to_float(a.get("LRS_UTM_Z")), province=prov,
            )
        return out

    # ---- Lluvia --------------------------------------------------------------------------------
    def _fetch_day(self, day: date) -> list[dict]:
        try:
            r = self.http.get(API_URL, params={"tipo_senal": "PACUM", "inicio": day.isoformat(),
                                               "apikey": self.api_key}, timeout=120)
        except requests.RequestException as e:
            # Sin la URL: lleva la clave y el error se guarda en BD y se enseña en la web
            raise RuntimeError(f"SAIH Ebro {day}: {type(e).__name__}") from None
        if r.status_code != 200:
            raise RuntimeError(f"SAIH Ebro {day}: HTTP {r.status_code} {r.text[:200].strip()}")
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"SAIH Ebro {day}: respuesta no JSON: {r.text[:200].strip()}") from None
        if not isinstance(data, dict) or "senales" not in data:
            raise RuntimeError(f"SAIH Ebro {day}: {str(data)[:200]}")
        return data["senales"]

    def _day(self, day: date, today: date) -> tuple[dict[str, float], set[str], set[str]]:
        """Totales de un día, de la caché si ya está cerrado."""
        path = CACHE / "daily" / f"{day.isoformat()}.json"
        if path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("qc") == QC_VERSION:  # cachés con otro filtro de averías se vuelven a pedir
                return cached["mm"], set(cached["complete"]), set(cached["suspect"])
        totals, complete, suspect = daily_totals(self._fetch_day(day), day)
        # Se guarda solo si el día terminó hace rato y trae el cierre de casi todas las estaciones
        if day <= today - timedelta(days=2) and totals and len(complete) >= 0.9 * len(totals):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"mm": totals, "complete": sorted(complete),
                                        "suspect": sorted(suspect), "qc": QC_VERSION}), encoding="utf-8")
        return totals, complete, suspect

    def fetch(self, start: date, end: date) -> CollectorResult:
        if not self.api_key:
            raise MissingCredentials("Falta SAIH_API_KEY en .env")
        res = CollectorResult(self.source)
        stations = self._stations()
        today = date.today()
        start = max(start, today - timedelta(days=MAX_DAYS_BACK - 1))
        end = min(end, today)

        seen: set[str] = set()
        day = end  # de más reciente a más antiguo: si algo corta, lo último ya está guardado
        while day >= start:
            try:
                totals, complete, suspect = self._day(day, today)
            except Exception as e:  # noqa: BLE001 - un día fallido no tumba el resto
                res.errors.append(str(e))
                log.warning("%s", e)
                if len(res.errors) >= 3 and not res.observations:
                    raise RuntimeError("; ".join(res.errors)) from None
                day -= timedelta(days=1)
                continue
            for code in sorted(suspect & stations.keys()):
                res.errors.append(f"{day} {stations[code].name}: descartado (patrón de avería)")
                res.discard.append((f"{self.source}:{code}", day))
            for code, mm in totals.items():
                if code not in stations:
                    continue
                seen.add(code)
                res.observations.append(Observation(
                    f"{self.source}:{code}", self.source, day, mm, partial=code not in complete))
            day -= timedelta(days=1)

        res.stations = [s for c, s in stations.items() if c in seen]
        return res
