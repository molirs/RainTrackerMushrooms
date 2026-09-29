"""Meteocat (XEMA) vía Dades Obertes de la Generalitat (Socrata, sin clave).

- Metadatos de estaciones: yqwd-vj5e
- Datos diarios (variable 1300 = precipitación acumulada diaria, 00-24 UTC): 7bvh-jvq2.
  Suele ir con ~2 días de retraso.
- Datos semihorarios (variable 35 = precipitación 30 min): nzvn-apee. Se usan para los días
  que aún no están en el dataset diario, sumando por día UTC.
"""
import logging
from datetime import date, datetime, timedelta

from app.collectors.base import (Collector, CollectorResult, Observation, PoliteSession, Station,
                                 to_float)
from app.geo import normalize_province

log = logging.getLogger(__name__)

BASE = "https://analisi.transparenciacatalunya.cat/resource"
DS_STATIONS = "yqwd-vj5e"
DS_DAILY = "7bvh-jvq2"
DS_SEMIHOURLY = "nzvn-apee"
VAR_DAILY_PRECIP = "1300"
VAR_30MIN_PRECIP = "35"
PROVINCES = ("Lleida", "Tarragona")


class MeteocatCollector(Collector):
    source = "meteocat"
    label = "Meteocat (XEMA)"
    attribution = "© Servei Meteorològic de Catalunya (Meteocat) — Dades Obertes de la Generalitat"
    day_window = "00–24 h UTC"

    def __init__(self, session: PoliteSession | None = None):
        self.http = session or PoliteSession(min_interval=0.5)

    def _query(self, dataset: str, **params) -> list[dict]:
        params.setdefault("$limit", 50000)
        r = self.http.get(f"{BASE}/{dataset}.json", params=params)
        r.raise_for_status()
        return r.json()

    def fetch(self, start: date, end: date) -> CollectorResult:
        res = CollectorResult(self.source)

        prov_list = ",".join(f"'{p}'" for p in PROVINCES)
        rows = self._query(DS_STATIONS, **{"$where": f"nom_provincia in ({prov_list})"})
        codes = []
        for r in rows:
            if r.get("nom_estat_ema") != "Operativa":
                continue
            code = r["codi_estacio"]
            codes.append(code)
            res.stations.append(Station(
                station_id=f"{self.source}:{code}", source=self.source, name=r["nom_estacio"],
                lat=float(r["latitud"]), lon=float(r["longitud"]),
                altitude=to_float(r.get("altitud")),
                province=normalize_province(r.get("nom_provincia")),
            ))
        if not codes:
            res.errors.append("No se han obtenido estaciones")
            return res
        code_list = ",".join(f"'{c}'" for c in codes)

        # 1) Diarios consolidados
        daily = self._query(DS_DAILY, **{
            "$select": "codi_estacio,data_lectura,valor",
            "$where": (f"codi_variable='{VAR_DAILY_PRECIP}' AND codi_estacio in ({code_list}) "
                       f"AND data_lectura between '{start.isoformat()}T00:00:00' "
                       f"and '{end.isoformat()}T00:00:00'"),
        })
        have: set[tuple[str, date]] = set()
        last_daily = None
        for r in daily:
            v = to_float(r.get("valor"))
            if v is None:
                continue
            d = datetime.fromisoformat(r["data_lectura"]).date()
            have.add((r["codi_estacio"], d))
            last_daily = max(last_daily or d, d)
            res.observations.append(Observation(f"{self.source}:{r['codi_estacio']}", self.source, d, v))

        # 2) Días posteriores al último diario: sumar la precipitación semihoraria por día UTC
        gap_start = (last_daily + timedelta(days=1)) if last_daily else start
        if gap_start <= end:
            semi = self._query(DS_SEMIHOURLY, **{
                "$select": "codi_estacio, date_trunc_ymd(data_lectura) as dia, "
                           "sum(valor_lectura) as mm, count(*) as n",
                "$where": (f"codi_variable='{VAR_30MIN_PRECIP}' AND codi_estacio in ({code_list}) "
                           f"AND data_lectura >= '{gap_start.isoformat()}T00:00:00' "
                           f"AND data_lectura < '{(end + timedelta(days=1)).isoformat()}T00:00:00'"),
                "$group": "codi_estacio, dia",
            })
            for r in semi:
                d = datetime.fromisoformat(r["dia"]).date()
                if (r["codi_estacio"], d) in have:
                    continue
                v = to_float(r.get("mm"))
                if v is None:
                    continue
                partial = int(r.get("n", 0)) < 48
                res.observations.append(Observation(
                    f"{self.source}:{r['codi_estacio']}", self.source, d, round(v, 1), partial=partial))
        return res
