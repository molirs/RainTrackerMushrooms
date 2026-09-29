"""InfoRiego (ITACyL, Junta de Castilla y León): ficheros diarios abiertos, sin clave, CC BY 4.0.

Un ZIP por día con un CSV (';', latin-1) de todas las estaciones de CyL; nos quedamos con Soria (SO*).
El fichero del día se publica hacia las 13:30 del día siguiente y se reescribe al otro día:
re-descargar los últimos días en cada ejecución. El servidor devuelve 403 sin User-Agent de navegador.
Coordenadas de estaciones en ED50 (desfase ~100-200 m respecto a WGS84, irrelevante aquí).
"""
import csv
import io
import zipfile
from datetime import date, timedelta

from app.collectors.base import Collector, CollectorResult, Observation, PoliteSession, Station, to_float

BASE = "http://ftp.itacyl.es/Meteorologia/Datos_observacion_Red_InfoRiego"
STATIONS_URL = f"{BASE}/Situacion/UbicacionEstacionesITACyL%202009.csv"
PROVINCE_PREFIX = {"SO": "Soria"}
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RadarSetas/0.1"


def parse_dms(value: str) -> float:
    """'022912000W' -> -(2 + 29/60 + 12.000/3600)  (DDDMMSSsss o DDMMSSsss + hemisferio)"""
    hemi, digits = value[-1].upper(), value[:-1]
    secs = int(digits[-5:]) / 1000
    mins = int(digits[-7:-5])
    deg = int(digits[:-7])
    dec = deg + mins / 60 + secs / 3600
    return -dec if hemi in "SW" else dec


class InfoRiegoCollector(Collector):
    source = "inforiego"
    label = "InfoRiego (ITACyL)"
    attribution = "InfoRiego – ITACyL, Junta de Castilla y León (CC BY 4.0)"
    day_window = "00–24 h"

    def __init__(self, session: PoliteSession | None = None):
        self.http = session or PoliteSession(min_interval=1.0)
        self.http.headers["User-Agent"] = BROWSER_UA

    def _stations(self) -> dict[str, Station]:
        r = self.http.get(STATIONS_URL)
        r.raise_for_status()
        out = {}
        for row in csv.DictReader(io.StringIO(r.content.decode("latin-1")), delimiter=";"):
            code = row["SHORT NAME"].strip()
            prov = PROVINCE_PREFIX.get(code[:2])
            if not prov:
                continue
            out[code] = Station(
                station_id=f"{self.source}:{code}", source=self.source, name=row["NAME"].strip(),
                lat=parse_dms(row["LATITUD ED50 DDMMSS.SSS"]), lon=parse_dms(row["LONGITUD ED50 DDMMSS.SSS"]),
                altitude=to_float(row.get("HEIGHT ASL (m)")), province=prov,
                # SiAR republica estas estaciones con el mismo código; las coordenadas ED50 de
                # InfoRiego difieren hasta ~400 m, así que la distancia sola no basta
                match_keys=(f"siar-code:{code}",),
            )
        return out

    def fetch(self, start: date, end: date) -> CollectorResult:
        res = CollectorResult(self.source)
        stations = self._stations()
        res.stations = list(stations.values())
        day = start
        while day <= min(end, date.today() - timedelta(days=1)):  # el de hoy sale vacío
            url = f"{BASE}/DatosDiarios/{day:%Y}/{day:%Y%m%d}_RedClimaITACYL_Diario.zip"
            try:
                r = self.http.get(url)
                if r.status_code == 404:
                    day += timedelta(days=1)
                    continue  # aún no publicado
                r.raise_for_status()
                z = zipfile.ZipFile(io.BytesIO(r.content))
                text = z.read(z.namelist()[0]).decode("latin-1")
            except Exception as e:  # noqa: BLE001
                res.errors.append(f"{day}: {e}")
                day += timedelta(days=1)
                continue
            rows = csv.reader(io.StringIO(text), delimiter=";")
            next(rows, None)
            for row in rows:
                if len(row) < 4 or row[0] not in stations:
                    continue
                v = to_float(row[3])
                if v is None:
                    continue
                res.observations.append(Observation(
                    stations[row[0]].station_id, self.source, date.fromisoformat(row[2]), round(v, 1)))
            day += timedelta(days=1)
        return res
