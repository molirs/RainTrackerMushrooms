"""Tipos comunes y utilidades HTTP para todos los colectores."""
import logging
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import certifi
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.config import CACHE_DIR, USER_AGENT

log = logging.getLogger(__name__)


@dataclass
class Station:
    station_id: str          # id único global: "<source>:<id_original>"
    source: str
    name: str
    lat: float
    lon: float
    altitude: float | None
    province: str | None
    estimated: bool = False  # True para puntos de rejilla (Open-Meteo), no medidos
    day_window: str | None = None  # qué horas cubre el "día" de esta estación; None = el de su colector
    # Claves que identifican la MISMA estación publicada por otra red (p. ej. "siar-code:SO01").
    # Dos estaciones de distinta fuente con una clave común se tratan como duplicadas.
    match_keys: tuple[str, ...] = ()


@dataclass
class Observation:
    station_id: str
    source: str
    date: date
    precip_mm: float
    partial: bool = False    # día en curso o incompleto (sumado de datos horarios parciales)


@dataclass
class HourlyObservation:
    """Lluvia de un intervalo que termina en ts (UTC). Para fuentes que solo dan las últimas horas:
    se guardan en BD y el día parcial se recalcula sumándolas, sin perder horas entre ejecuciones."""
    station_id: str
    source: str
    ts: datetime
    precip_mm: float


@dataclass
class CollectorResult:
    source: str
    stations: list[Station] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    hourly: list[HourlyObservation] = field(default_factory=list)
    # Hora UTC en la que empieza el "día" al agregar los horarios (AEMET: 7, igual que su diario 07-07)
    hourly_day_start_utc: int = 0
    errors: list[str] = field(default_factory=list)
    # (station_id, fecha) cuyo dato se ha descartado por control de calidad: se borra si ya estaba en BD
    discard: list[tuple[str, date]] = field(default_factory=list)

    def rows(self):
        """Filas normalizadas: station_id, source, name, lat, lon, altitude, province, date, precip_mm."""
        by_id = {s.station_id: s for s in self.stations}
        for o in self.observations:
            s = by_id.get(o.station_id)
            if s is None:
                continue
            yield {
                "station_id": s.station_id, "source": s.source, "name": s.name,
                "lat": s.lat, "lon": s.lon, "altitude": s.altitude, "province": s.province,
                "date": o.date.isoformat(), "precip_mm": o.precip_mm,
            }


class MissingCredentials(Exception):
    pass


class Collector:
    source: str = ""
    label: str = ""             # nombre para mostrar
    attribution: str = ""       # texto de titularidad de los datos
    day_window: str = ""        # qué horas cubre el valor diario, p. ej. "00–24 h UTC"
    required_env: tuple[str, ...] = ()

    def fetch(self, start: date, end: date) -> CollectorResult:
        raise NotImplementedError


def ca_bundle() -> str:
    """certifi + intermedios que algunos servidores públicos no envían (p. ej. FNMT en mapa.gob.es)."""
    target = CACHE_DIR / "ca-bundle.pem"
    extra = sorted((Path(__file__).parent.parent / "data" / "certs").glob("*.pem"))
    if not target.exists() or any(p.stat().st_mtime > target.stat().st_mtime for p in extra):
        target.parent.mkdir(parents=True, exist_ok=True)
        parts = [Path(certifi.where()).read_text(encoding="ascii")] + [p.read_text(encoding="ascii") for p in extra]
        target.write_text("\n".join(parts), encoding="ascii")
    return str(target)


class PoliteSession(requests.Session):
    """Session con reintentos, User-Agent propio y un intervalo mínimo entre peticiones por host."""

    def __init__(self, min_interval: float = 1.0, retries: int = 3):
        super().__init__()
        self.headers["User-Agent"] = USER_AGENT
        self.verify = ca_bundle()
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        retry = Retry(total=retries, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET", "POST"), respect_retry_after_header=True)
        self.mount("https://", HTTPAdapter(max_retries=retry))
        self.mount("http://", HTTPAdapter(max_retries=retry))

    def request(self, method, url, *args, **kwargs):
        host = urlparse(url).netloc
        with self._lock:
            wait = self._last.get(host, 0) + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()
        kwargs.setdefault("timeout", 60)
        return super().request(method, url, *args, **kwargs)


def to_float(value) -> float | None:
    """'1,2' -> 1.2 ; 'Ip' (inapreciable) -> 0.0 ; ''/None/'Acum' -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    v = str(value).strip().replace(",", ".")
    if v.lower() == "ip":
        return 0.0
    try:
        return float(v)
    except ValueError:
        return None


def as_dict(obj):
    return asdict(obj)


def name_key(name: str) -> str:
    """Nombre normalizado para emparejar estaciones entre redes: sin tildes, minúsculas, espacios simples."""
    s = "".join(c for c in unicodedata.normalize("NFD", name) if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())
