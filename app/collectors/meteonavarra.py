"""Meteo Navarra (Gobierno de Navarra; redes GN, INTIA, MAPA, AEMET, UPNA). Sin API: scraping respetuoso.

- Coordenadas: capas WFS de IDENA (GeoJSON WGS84). Están desactualizadas: las estaciones que faltan
  se completan con la ficha estacion_detalle.cfm (UTM 30N ETRS89) y se cachean en disco.
- Lluvia diaria: mapasdatosdiaspasados.cfm?estaciones=AUTO|MAN&fecha_sel=DD/MM/YYYY&IDParam_sel=1007
  (una petición por día y red). Las manuales miden de 08 a 08 h (se asignan al día en que empieza).
- Datos provisionales; el día en curso sale vacío. Re-descargar los últimos días en cada ejecución.
"""
import html
import json
import re
import time
from datetime import date, timedelta

from app.collectors.base import (Collector, CollectorResult, Observation, PoliteSession, Station, name_key,
                                 to_float)
from app.config import CACHE_DIR
from app.geo import utm_to_latlon

SITE = "https://meteo.navarra.es/estaciones"
WFS = ("https://idena.navarra.es/ogc/wfs?service=WFS&version=2.0.0&request=GetFeature"
       "&outputFormat=application/json&srsName=EPSG:4326&typeNames=IDENA:{layer}")
LAYERS = ("METEOR_Sym_EstMetAuto", "METEOR_Sym_EstMetManu")
NETWORKS = ("AUTO", "MAN")
CACHE_FILE = CACHE_DIR / "meteonavarra_stations.json"
CACHE_TTL = 7 * 86400

ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
CELL_RE = re.compile(r'IDestacion=(\d+)"[^>]*>([^<]+)</a>.*?<td align="right">(?:<b[^>]*>([^<]*)</b>)?</td>', re.S)
SIAR_SUFFIX_RE = re.compile(r"\s+(MAPA|INTIA)\s*$")
DETAIL_RE = re.compile(r"Altitud:\s*([\d.,]+)\s*m.*?X:\s*([\d.,]+).*?Y:\s*([\d.,]+)", re.S)


class MeteoNavarraCollector(Collector):
    source = "meteonavarra"
    label = "Meteo Navarra"
    attribution = "Meteo Navarra – Gobierno de Navarra (datos provisionales)"
    day_window = "00–24 h"
    manual_day_window = "08–08 h (del día indicado al siguiente)"

    def __init__(self, session: PoliteSession | None = None):
        self.http = session or PoliteSession(min_interval=1.5)

    # --- estaciones -------------------------------------------------------------------------
    def _load_cache(self) -> dict:
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - data.get("_ts", 0) < CACHE_TTL:
                return data
        except (OSError, ValueError):
            pass
        return {"_ts": time.time()}

    def _wfs_stations(self, cache: dict):
        if cache.get("_wfs"):
            return
        for layer in LAYERS:
            r = self.http.get(WFS.format(layer=layer))
            r.raise_for_status()
            for f in r.json()["features"]:
                p = f["properties"]
                lon, lat = f["geometry"]["coordinates"][:2]
                cache[str(p["CESTACION"])] = {"lat": lat, "lon": lon, "alt": p.get("ALTITUD")}
        cache["_wfs"] = True

    def _detail(self, sid: str) -> dict | None:
        r = self.http.get(f"{SITE}/estacion_detalle.cfm", params={"IDestacion": sid})
        r.encoding = "utf-8"
        m = DETAIL_RE.search(r.text)
        if not m:
            return None
        alt, x, y = (float(v.replace(",", ".")) for v in m.groups())
        lat, lon = utm_to_latlon(x, y, zone=30)
        return {"lat": lat, "lon": lon, "alt": alt}

    # --- datos ------------------------------------------------------------------------------
    def _day(self, network: str, day: date) -> list[tuple[str, str, float | None]]:
        r = self.http.get(f"{SITE}/mapasdatosdiaspasados.cfm", params={
            "estaciones": network, "fecha_sel": day.strftime("%d/%m/%Y"), "IDParam_sel": 1007})
        r.raise_for_status()
        r.encoding = "utf-8"
        out = []
        # Fila a fila: una celda vacía no debe capturar el valor de la estación siguiente
        for row in ROW_RE.findall(r.text):
            m = CELL_RE.search(row)
            if m:
                out.append((m[1], html.unescape(m[2]).strip(), to_float((m[3] or "").strip() or None)))
        return out

    def fetch(self, start: date, end: date) -> CollectorResult:
        res = CollectorResult(self.source)
        cache = self._load_cache()
        self._wfs_stations(cache)

        names: dict[str, str] = {}
        manual: set[str] = set()
        values: list[tuple[str, date, float]] = []
        day = start
        last = min(end, date.today() - timedelta(days=1))
        while day <= last:
            for network in NETWORKS:
                try:
                    rows = self._day(network, day)
                except Exception as e:  # noqa: BLE001
                    res.errors.append(f"{network} {day}: {e}")
                    continue
                for sid, name, v in rows:
                    names[sid] = name
                    if network == "MAN":
                        manual.add(sid)
                    if v is not None:
                        values.append((sid, day, v))
            day += timedelta(days=1)

        for sid, name in names.items():
            meta = cache.get(sid)
            if meta is None:
                try:
                    meta = self._detail(sid)
                except Exception as e:  # noqa: BLE001
                    res.errors.append(f"ficha {sid}: {e}")
                    meta = None
                if meta is None:
                    continue
                cache[sid] = meta
            # Las estaciones "X MAPA" / "X INTIA" son las mismas que publica SiAR
            m = SIAR_SUFFIX_RE.search(name)
            keys = (f"siar-name:{name_key(name[:m.start()])}",) if m and sid not in manual else ()
            res.stations.append(Station(
                station_id=f"{self.source}:{sid}", source=self.source, name=name,
                lat=meta["lat"], lon=meta["lon"], altitude=to_float(meta.get("alt")), province="Navarra",
                day_window=self.manual_day_window if sid in manual else None, match_keys=keys,
            ))
        known = {s.station_id for s in res.stations}
        for sid, d, v in values:
            station_id = f"{self.source}:{sid}"
            if station_id in known:
                res.observations.append(Observation(station_id, self.source, d, v))

        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        return res
