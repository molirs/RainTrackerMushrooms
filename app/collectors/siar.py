"""SiAR (MAPA): red agroclimática de regadío. Datos con token; lista de estaciones sin clave.

Manual: https://servicio.mapa.gob.es/siarweb/documentos/manuales/ManualTecnico_WEB_API_SIAR.pdf (v2.2)
- Token: SIAR_TOKEN, o bien SIAR_USER (NIF) + SIAR_PASSWORD, que se cifran con
  Autenticacion/cifrarCadena y se canjean en Autenticacion/obtenerToken.
- Datos: Datos/Diarios/PROVINCIA?token&Id=..&Id=..&FechaInicial&FechaFinal -> campo 'Precipitacion' (mm)
- Límites orientativos: 30 peticiones/min, 1000/día (consultar Info/ACCESOS). Una sola llamada por
  ejecución cubre todas las provincias.
- Estaciones sin clave: WMS GetFeatureInfo de mapama (GeoJSON con coordenadas en orden [lat, lon]).
PENDIENTE DE PROBAR con credenciales reales.
"""
import os
import re
from datetime import date

from app import config
from app.collectors.base import (Collector, CollectorResult, MissingCredentials, Observation,
                                 PoliteSession, Station, name_key, to_float)
from app.geo import normalize_province

API = "https://servicio.mapa.gob.es/siarapi/API/V1"
WMS = ("https://wms.mapama.gob.es/sig/DesarrolloRural/InfoAgroclimatica/Estaciones?SERVICE=WMS&VERSION=1.3.0"
       "&REQUEST=GetFeatureInfo&LAYERS=Estaciones&QUERY_LAYERS=Estaciones&STYLES=&CRS=EPSG:4326"
       "&BBOX=27,-19,44.5,5&WIDTH=1&HEIGHT=1&I=0&J=0&INFO_FORMAT=application/json&FEATURE_COUNT=10000&BUFFER=2000")


class SiarCollector(Collector):
    source = "siar"
    label = "SiAR (MAPA)"
    attribution = "SiAR – Ministerio de Agricultura, Pesca y Alimentación"
    day_window = "00–24 h"
    required_env = ("SIAR_TOKEN",)

    def __init__(self, session: PoliteSession | None = None):
        self.http = session or PoliteSession(min_interval=2.5)

    def _token(self) -> str:
        if config.SIAR_TOKEN:
            return config.SIAR_TOKEN
        user, pwd = os.getenv("SIAR_USER", "").strip(), os.getenv("SIAR_PASSWORD", "").strip()
        if not (user and pwd):
            raise MissingCredentials("Falta SIAR_TOKEN (o SIAR_USER + SIAR_PASSWORD) en .env")

        def cifrar(s):
            r = self.http.get(f"{API}/Autenticacion/cifrarCadena", params={"cadena": s})
            r.raise_for_status()
            return r.text.strip().strip('"')

        r = self.http.get(f"{API}/Autenticacion/obtenerToken",
                          params={"Usuario": cifrar(user), "Password": cifrar(pwd)})
        r.raise_for_status()
        return r.text.strip().strip('"')

    def _stations(self) -> dict[str, Station]:
        r = self.http.get(WMS)
        r.raise_for_status()
        out = {}
        for f in r.json().get("features", []):
            p = f["properties"]
            prov = normalize_province(p.get("Provincia"))
            if not prov or p.get("Estado") != "Activa":
                continue
            lat, lon = f["geometry"]["coordinates"][:2]  # ojo: orden [lat, lon]
            code = p["Id. Estación"]
            name = p.get("Denominación", code)
            # En Navarra se llaman "08 Miranda de Arga"; Meteo Navarra republica las mismas como
            # "Miranda de Arga MAPA" / "... INTIA" (a veces con coordenadas que difieren >1 km)
            keys = [f"siar-code:{code}"]
            if prov == "Navarra":
                keys.append("siar-name:" + name_key(re.sub(r"^\d+\s+", "", name)))
            out[code] = Station(
                station_id=f"{self.source}:{code}", source=self.source, name=name,
                lat=float(lat), lon=float(lon), altitude=to_float(p.get("Altura (m)")), province=prov,
                match_keys=tuple(keys),
            )
        return out

    def fetch(self, start: date, end: date) -> CollectorResult:
        token = self._token()
        res = CollectorResult(self.source)
        stations = self._stations()
        res.stations = list(stations.values())

        r = self.http.get(f"{API}/Info/PROVINCIAS", params={"token": token})
        r.raise_for_status()
        codes = [p["Codigo"] for p in r.json().get("datos", []) if normalize_province(p.get("Provincia"))]
        if not codes:
            res.errors.append("No se han encontrado los códigos de provincia")
            return res

        r = self.http.get(f"{API}/Datos/Diarios/PROVINCIA", params=[
            ("token", token), *[("Id", c) for c in codes],
            ("FechaInicial", start.isoformat()), ("FechaFinal", end.isoformat())])
        r.raise_for_status()
        body = r.json()
        if body.get("MensajeRespuesta"):
            res.errors.append(str(body["MensajeRespuesta"]))
        for row in body.get("datos") or []:
            code = row.get("Estacion")
            v = to_float(row.get("Precipitacion"))
            if code not in stations or v is None:
                continue
            res.observations.append(Observation(
                stations[code].station_id, self.source, date.fromisoformat(str(row["Fecha"])[:10]), v))
        return res
