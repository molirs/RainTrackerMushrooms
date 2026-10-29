"""Lógica de la API, sin depender de FastAPI: la usan el servidor (app/main.py) y la app Android
(app/mobile.py), que atiende las mismas rutas dentro del móvil."""
from datetime import date, timedelta
from pathlib import Path

from app import db
from app.collectors import COLLECTORS
from app.geo import PROVINCES

MAX_WINDOW_DAYS = 31
PROVINCES_GEOJSON = Path(__file__).parent / "data" / "provinces.geojson"


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def csv(value: str | None) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else []


def _station_filter(provinces, sources, alt_min, alt_max, estimated, duplicates):
    where, params = [], []
    if provinces:
        where.append(f"s.province IN ({','.join('?' * len(provinces))})")
        params += provinces
    if sources:
        # Una estación cuenta como de una fuente si la publica ella o alguna de sus duplicadas
        marks = ",".join("?" * len(sources))
        where.append(f"(s.source IN ({marks}) OR EXISTS (SELECT 1 FROM stations d "
                     f"WHERE d.duplicate_of = s.station_id AND d.source IN ({marks})))")
        params += sources + sources
    if alt_min is not None:
        where.append("s.altitude >= ?")
        params.append(alt_min)
    if alt_max is not None:
        where.append("s.altitude <= ?")
        params.append(alt_max)
    if not estimated:
        where.append("s.estimated = 0")
    if not duplicates:
        where.append("s.duplicate_of IS NULL")
    return (" AND ".join(where) or "1=1"), params


def _dates(from_: date | None, to: date | None, days: int | None) -> tuple[date, date]:
    if days is not None and not 1 <= days <= MAX_WINDOW_DAYS:
        raise ApiError(400, f"'days' debe estar entre 1 y {MAX_WINDOW_DAYS}")
    to = to or date.today()
    from_ = from_ or (to - timedelta(days=(days or MAX_WINDOW_DAYS) - 1))
    if from_ > to:
        raise ApiError(400, "'from' posterior a 'to'")
    if (to - from_).days + 1 > MAX_WINDOW_DAYS:
        raise ApiError(400, f"El intervalo máximo es de {MAX_WINDOW_DAYS} días")
    return from_, to


# Fuentes de las duplicadas de cada estación principal (para filtrar por fuente en el cliente)
DUP_SOURCES_SQL = ("(SELECT GROUP_CONCAT(DISTINCT d.source) FROM stations d "
                   "WHERE d.duplicate_of = s.station_id) AS dup_sources")


def _station_json(r) -> dict:
    out = {
        "id": r["station_id"], "source": r["source"], "name": r["name"],
        "lat": r["lat"], "lon": r["lon"], "altitude": r["altitude"], "province": r["province"],
        "estimated": bool(r["estimated"]), "day_window": r["day_window"],
    }
    if "dup_sources" in r.keys():
        out["sources"] = [r["source"], *[x for x in (r["dup_sources"] or "").split(",") if x]]
    return out


def sources() -> list[dict]:
    with db.session() as conn:
        status = {r["source"]: dict(r) for r in conn.execute("SELECT * FROM source_status")}
    return [{
        "id": c.source, "label": c.label, "attribution": c.attribution,
        "estimated": c.source == "openmeteo", "status": status.get(c.source),
    } for c in COLLECTORS.values()]


def provinces_geojson() -> str:
    return PROVINCES_GEOJSON.read_text(encoding="utf-8")


def stations(provinces: str | None = None, sources: str | None = None,
             alt_min: float | None = None, alt_max: float | None = None,
             estimated: bool = False, duplicates: bool = False) -> list[dict]:
    where, params = _station_filter(csv(provinces), csv(sources), alt_min, alt_max, estimated, duplicates)
    with db.session() as conn:
        rows = conn.execute(f"SELECT s.*, {DUP_SOURCES_SQL} FROM stations s WHERE {where} ORDER BY s.name",
                            params).fetchall()
    return [_station_json(r) for r in rows]


def rain(from_: date | None = None, to: date | None = None, days: int | None = None,
         provinces: str | None = None, sources: str | None = None,
         alt_min: float | None = None, alt_max: float | None = None,
         min_mm: float = 0, estimated: bool = False) -> dict:
    """Lluvia acumulada por estación entre from y to (ambos incluidos)."""
    from_, to = _dates(from_, to, days)
    where, params = _station_filter(csv(provinces), csv(sources), alt_min, alt_max, estimated, False)
    n_days = (to - from_).days + 1
    with db.session() as conn:
        rows = conn.execute(f"""
            SELECT s.*, ROUND(SUM(o.precip_mm), 1) AS total_mm, COUNT(o.date) AS n_days,
                   SUM(o.partial) AS n_partial, SUM(o.from_station != s.station_id) AS n_filled,
                   MAX(o.date) AS last_date
            FROM stations s JOIN effective_observations o ON o.station_id = s.station_id
            WHERE {where} AND o.date BETWEEN ? AND ?
            GROUP BY s.station_id
            HAVING total_mm >= ?
            ORDER BY total_mm DESC""", [*params, from_.isoformat(), to.isoformat(), min_mm]).fetchall()
    return {
        "from": from_.isoformat(), "to": to.isoformat(), "days": n_days,
        "stations": [{**_station_json(r), "total_mm": r["total_mm"], "days_with_data": r["n_days"],
                      "partial_days": r["n_partial"], "filled_days": r["n_filled"],
                      "last_date": r["last_date"]} for r in rows],
    }


def rain_daily(from_: date | None = None, to: date | None = None, days: int | None = None,
               provinces: str | None = None, sources: str | None = None,
               alt_min: float | None = None, alt_max: float | None = None, estimated: bool = True) -> dict:
    """Matriz estación × día (mm, null = sin dato). Pensada para que el frontend anime día a día y
    calcule acumulados sin más peticiones. 'filled' = días rellenados con una duplicada de otra red."""
    from_, to = _dates(from_, to, days)
    dates = [(from_ + timedelta(days=i)).isoformat() for i in range((to - from_).days + 1)]
    index = {d: i for i, d in enumerate(dates)}
    where, params = _station_filter(csv(provinces), csv(sources), alt_min, alt_max, estimated, False)
    series: dict[str, list] = {}
    partial: dict[str, list[int]] = {}
    filled: dict[str, list[int]] = {}
    with db.session() as conn:
        for r in conn.execute(f"""
                SELECT o.station_id, o.date, o.precip_mm, o.partial, o.from_station
                FROM effective_observations o JOIN stations s ON s.station_id = o.station_id
                WHERE {where} AND o.date BETWEEN ? AND ?""",
                [*params, from_.isoformat(), to.isoformat()]):
            row = series.setdefault(r["station_id"], [None] * len(dates))
            row[index[r["date"]]] = r["precip_mm"]
            if r["partial"]:
                partial.setdefault(r["station_id"], []).append(index[r["date"]])
            if r["from_station"] != r["station_id"]:
                filled.setdefault(r["station_id"], []).append(index[r["date"]])
    return {"dates": dates, "series": series, "partial": partial, "filled": filled}


def station_series(station_id: str, days: int = MAX_WINDOW_DAYS, to: date | None = None) -> dict:
    from_, to = _dates(None, to, days)
    with db.session() as conn:
        s = conn.execute("SELECT * FROM stations WHERE station_id = ?", (station_id,)).fetchone()
        if not s:
            raise ApiError(404, "Estación no encontrada")
        if s["duplicate_of"]:  # se pide una duplicada: se muestra la estación principal
            return station_series(s["duplicate_of"], days, to)
        obs = {r["date"]: r for r in conn.execute(
            """SELECT date, precip_mm, partial, from_station, from_source FROM effective_observations
               WHERE station_id = ? AND date BETWEEN ? AND ?""",
            (station_id, from_.isoformat(), to.isoformat()))}
        dup = conn.execute("SELECT station_id, source, name, day_window FROM stations WHERE duplicate_of = ?",
                           (station_id,)).fetchall()
    out, acc = [], 0.0
    for i in range((to - from_).days + 1):
        d = (from_ + timedelta(days=i)).isoformat()
        o = obs.get(d)
        mm = o["precip_mm"] if o else None
        acc += mm or 0
        filled_from = o["from_source"] if o and o["from_station"] != station_id else None
        out.append({"date": d, "mm": mm, "partial": bool(o and o["partial"]), "filled_from": filled_from,
                    "cum_mm": round(acc, 1)})
    return {**_station_json(s), "also_in": [dict(r) for r in dup], "days": out}


def meta() -> dict:
    return {"provinces": list(PROVINCES.values()), "max_window_days": MAX_WINDOW_DAYS,
            "today": date.today().isoformat()}
