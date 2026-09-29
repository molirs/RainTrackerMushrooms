"""SIAR La Rioja (Gobierno de La Rioja), API pública sin clave, CC BY 4.0.

Docs: https://ias1.larioja.org/opendata/download?r=Y2Q9OTc2fGNmPTE2
- GET /estaciones -> {"estaciones": [...]}, coordenadas WGS84 en decimal (todo viene como string)
- GET /datos-climaticos/{codigo}/D?fecha_inicio&fecha_fin&parametro=P&funcion=Ac -> lluvia diaria (mm)
Una petición por estación. Los valores se revisan a posteriori: re-descargar la ventana completa.
Las listas de un solo elemento llegan como objeto suelto.
"""
from datetime import date

from app.collectors.base import Collector, CollectorResult, Observation, PoliteSession, Station, to_float

BASE = "https://ias1.larioja.org/apiSiar/servicios/v2"
VALID = {"V", "S"}  # N = no válido


def aslist(x):
    return x if isinstance(x, list) else ([] if x is None else [x])


class LaRiojaCollector(Collector):
    source = "larioja"
    label = "SIAR La Rioja"
    attribution = "SIAR – Gobierno de La Rioja (CC BY 4.0)"
    day_window = "00–24 h"  # comprobado: coincide con la suma de sus semihorarios [00:00, 24:00)

    def __init__(self, session: PoliteSession | None = None):
        self.http = session or PoliteSession(min_interval=0.5)

    def fetch(self, start: date, end: date) -> CollectorResult:
        res = CollectorResult(self.source)
        r = self.http.get(f"{BASE}/estaciones")
        r.raise_for_status()
        for s in aslist(r.json().get("estaciones")):
            code = str(s["codigo_estacion"])
            last = s.get("fecha_ultimo_diario")
            if not last or last[:10] < start.isoformat():
                continue  # estación sin datos diarios en el periodo (inactiva o de temporada)
            station = Station(
                station_id=f"{self.source}:{code}", source=self.source, name=s["nombre"],
                lat=float(s["latitud"]), lon=float(s["longitud"]),
                altitude=to_float(s.get("altitud")), province="La Rioja",
            )
            res.stations.append(station)
            try:
                d = self.http.get(f"{BASE}/datos-climaticos/{code}/D", params={
                    "fecha_inicio": start.isoformat(), "fecha_fin": end.isoformat(),
                    "parametro": "P", "funcion": "Ac",
                })
                d.raise_for_status()
                rows = aslist(d.json().get("datos"))
            except Exception as e:  # noqa: BLE001
                res.errors.append(f"{code}: {e}")
                continue
            for row in rows:
                v = to_float(row.get("valor"))
                if v is None or row.get("dato_valido") not in VALID:
                    continue
                res.observations.append(Observation(
                    station.station_id, self.source, date.fromisoformat(row["fecha"][:10]), round(v, 1)))
        return res
