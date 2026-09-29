"""Almacenamiento en SQLite (fácil de migrar a PostgreSQL más adelante)."""
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.collectors.base import CollectorResult
from app.config import ROOT
from app.geo import haversine_m

DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data" / "rain.sqlite"))
RETENTION_DAYS = 60   # CLAUDE.md pide al menos 30
DEDUP_RADIUS_M = 300          # misma estación si está a menos de esto...
DEDUP_MAX_ALT_DIFF_M = 30     # ...y su altitud no difiere más (evita fusionar instrumentos distintos)
MATCH_KEY_MAX_DIST_M = 3000   # comprobación de cordura al emparejar por código/nombre

# Menor = preferida al deduplicar (la fuente original frente a la que reenvía los datos)
SOURCE_PRIORITY = {
    "aemet": 1, "meteocat": 1, "larioja": 1, "inforiego": 1,
    "siar": 2, "saihebro": 2,
    "meteonavarra": 3,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    station_id   TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    name         TEXT NOT NULL,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    altitude     REAL,
    province     TEXT,
    estimated    INTEGER NOT NULL DEFAULT 0,
    duplicate_of TEXT,            -- si es la misma estación que otra de una fuente prioritaria
    day_window   TEXT,            -- horas que cubre su valor diario
    match_keys   TEXT,            -- claves separadas por espacio para emparejar entre redes
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_stations_source ON stations(source);

CREATE TABLE IF NOT EXISTS observations (
    station_id TEXT NOT NULL,
    date       TEXT NOT NULL,      -- YYYY-MM-DD
    precip_mm  REAL NOT NULL,
    partial    INTEGER NOT NULL DEFAULT 0,
    from_hourly INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (station_id, date)
);
CREATE INDEX IF NOT EXISTS ix_obs_date ON observations(date);

CREATE TABLE IF NOT EXISTS hourly (
    station_id TEXT NOT NULL,
    ts         TEXT NOT NULL,      -- fin del intervalo, ISO UTC
    precip_mm  REAL NOT NULL,
    PRIMARY KEY (station_id, ts)
);

CREATE TABLE IF NOT EXISTS source_status (
    source        TEXT PRIMARY KEY,
    last_attempt  TEXT,
    last_success  TEXT,
    last_error    TEXT,
    n_stations    INTEGER,
    n_observations INTEGER
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _priority_sql(col: str) -> str:
    cases = " ".join(f"WHEN '{src}' THEN {prio}" for src, prio in SOURCE_PRIORITY.items())
    return f"(CASE {col} {cases} ELSE 9 END)"


def init(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stations)")}
    for col in ("day_window", "match_keys"):  # BD creadas con versiones anteriores
        if col not in cols:
            conn.execute(f"ALTER TABLE stations ADD COLUMN {col} TEXT")
    # Dato efectivo por estación principal y día: si la principal no tiene dato (o solo parcial) se
    # rellena con el de sus duplicadas en otras redes. Preferencia: definitivo > parcial,
    # la propia estación > duplicadas, y después la prioridad de la fuente.
    view_sql = f"""CREATE VIEW effective_observations AS
        SELECT station_id, date, precip_mm, partial, from_station, from_source FROM (
            SELECT COALESCE(s.duplicate_of, s.station_id) AS station_id, o.date, o.precip_mm, o.partial,
                   o.station_id AS from_station, s.source AS from_source,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(s.duplicate_of, s.station_id), o.date
                       ORDER BY o.partial, s.duplicate_of IS NOT NULL, {_priority_sql('s.source')}, o.station_id
                   ) AS rn
            FROM observations o JOIN stations s ON s.station_id = o.station_id
        ) WHERE rn = 1"""
    current = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'effective_observations'").fetchone()
    if current is None or current[0] != view_sql:  # solo si falta o ha cambiado su definición
        try:
            conn.execute("DROP VIEW IF EXISTS effective_observations")
            conn.execute(view_sql)
            conn.commit()
        except sqlite3.OperationalError:
            conn.rollback()  # otro proceso la ha creado a la vez


_initialized: set[str] = set()
_init_lock = threading.Lock()


@contextmanager
def session(path: Path | None = None):
    conn = connect(path)
    try:
        key = str(Path(path or DB_PATH).resolve())
        with _init_lock:  # esquema y vista una vez por proceso (la API atiende peticiones en paralelo)
            if key not in _initialized:
                init(conn)
                _initialized.add(key)
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_result(conn: sqlite3.Connection, res: CollectorResult, day_window: str | None = None):
    ts = now_iso()
    conn.executemany(
        """INSERT INTO stations (station_id, source, name, lat, lon, altitude, province, estimated,
                                 day_window, match_keys, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(station_id) DO UPDATE SET
             name=excluded.name, lat=excluded.lat, lon=excluded.lon,
             altitude=COALESCE(excluded.altitude, stations.altitude),
             province=COALESCE(excluded.province, stations.province),
             estimated=excluded.estimated, day_window=excluded.day_window,
             match_keys=excluded.match_keys, updated_at=excluded.updated_at""",
        [(s.station_id, s.source, s.name, s.lat, s.lon, s.altitude, s.province, int(s.estimated),
          s.day_window or day_window, " ".join(s.match_keys) or None, ts)
         for s in res.stations],
    )
    # Un valor parcial nunca pisa uno definitivo
    conn.executemany(
        """INSERT INTO observations (station_id, date, precip_mm, partial, from_hourly, updated_at)
           VALUES (?,?,?,?,0,?)
           ON CONFLICT(station_id, date) DO UPDATE SET
             precip_mm=excluded.precip_mm, partial=excluded.partial, from_hourly=0,
             updated_at=excluded.updated_at
           WHERE observations.partial = 1 OR excluded.partial = 0""",
        [(o.station_id, o.date.isoformat(), o.precip_mm, int(o.partial), ts) for o in res.observations],
    )
    if res.hourly:
        conn.executemany(
            "INSERT OR REPLACE INTO hourly (station_id, ts, precip_mm) VALUES (?,?,?)",
            [(h.station_id, h.ts.astimezone(timezone.utc).isoformat(timespec="seconds"), h.precip_mm)
             for h in res.hourly],
        )
        _rebuild_from_hourly(conn, {h.station_id for h in res.hourly}, ts, res.hourly_day_start_utc)


def _rebuild_from_hourly(conn: sqlite3.Connection, station_ids: set[str], ts: str, day_start_utc: int = 0):
    """Rehace los días parciales sumando los horarios guardados (solo donde no hay dato definitivo).
    El día D va de D day_start_utc:00 a D+1 day_start_utc:00 UTC (AEMET: 07-07, como su diario)."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _ids (station_id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _ids")
    conn.executemany("INSERT INTO _ids VALUES (?)", [(s,) for s in station_ids])
    conn.execute("""DELETE FROM observations WHERE from_hourly = 1 AND partial = 1
                    AND station_id IN (SELECT station_id FROM _ids)""")
    # ts es el FINAL de la hora: la que termina justo al empezar el día pertenece al anterior
    shift = f"-{day_start_utc * 3600 + 1} seconds"
    conn.execute(
        """INSERT INTO observations (station_id, date, precip_mm, partial, from_hourly, updated_at)
           SELECT h.station_id, date(h.ts, ?) AS d, ROUND(SUM(h.precip_mm), 1),
                  1, 1, ?
           FROM hourly h JOIN _ids USING (station_id)
           WHERE true
           GROUP BY h.station_id, d
           ON CONFLICT(station_id, date) DO UPDATE SET
             precip_mm=excluded.precip_mm, from_hourly=1, updated_at=excluded.updated_at
           WHERE observations.partial = 1""",
        (shift, ts),
    )


def record_status(conn: sqlite3.Connection, source: str, *, ok: bool, error: str | None = None,
                  n_stations: int | None = None, n_observations: int | None = None):
    ts = now_iso()
    conn.execute(
        """INSERT INTO source_status (source, last_attempt, last_success, last_error, n_stations, n_observations)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(source) DO UPDATE SET
             last_attempt=excluded.last_attempt,
             last_success=COALESCE(excluded.last_success, source_status.last_success),
             last_error=excluded.last_error,
             n_stations=COALESCE(excluded.n_stations, source_status.n_stations),
             n_observations=COALESCE(excluded.n_observations, source_status.n_observations)""",
        (source, ts, ts if ok else None, error, n_stations, n_observations),
    )


def deduplicate(conn: sqlite3.Connection, radius_m: float = DEDUP_RADIUS_M,
                max_alt_diff: float = DEDUP_MAX_ALT_DIFF_M) -> int:
    """Agrupa las estaciones que son la misma aunque las publiquen redes distintas y marca como
    duplicate_of todas menos la de la fuente prioritaria. Dos estaciones de distinta fuente son la misma si:
      a) comparten una match_key (código o nombre declarado por su colector) y están a < 3 km, o
      b) están a < radius_m y su altitud no difiere más de max_alt_diff.
    Las estimadas (rejilla) no participan."""
    rows = [dict(r) for r in conn.execute(
        "SELECT station_id, source, lat, lon, altitude, match_keys FROM stations WHERE estimated = 0")]
    parent = {r["station_id"]: r["station_id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def dist(a, b):
        return haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])

    def link(a, b):
        parent[find(a["station_id"])] = find(b["station_id"])

    # a) Claves explícitas
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        for k in (r["match_keys"] or "").split():
            by_key.setdefault(k, []).append(r)
    for group in by_key.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if a["source"] != b["source"] and dist(a, b) < MATCH_KEY_MAX_DIST_M:
                    link(a, b)

    # b) Proximidad + altitud (rejilla de ~0.01° para no comparar todo con todo)
    buckets: dict[tuple[int, int], list[dict]] = {}
    for r in rows:
        buckets.setdefault((int(r["lat"] * 100), int(r["lon"] * 100)), []).append(r)
    for (by, bx), members in buckets.items():
        near = [o for dy in (-1, 0, 1) for dx in (-1, 0, 1) for o in buckets.get((by + dy, bx + dx), [])]
        for a in members:
            for b in near:
                if a["station_id"] >= b["station_id"] or a["source"] == b["source"]:
                    continue
                if a["altitude"] is not None and b["altitude"] is not None \
                        and abs(a["altitude"] - b["altitude"]) > max_alt_diff:
                    continue
                if dist(a, b) < radius_m:
                    link(a, b)

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(find(r["station_id"]), []).append(r)
    updates = []
    for members in groups.values():
        primary = min(members, key=lambda r: (SOURCE_PRIORITY.get(r["source"], 9), r["station_id"]))
        updates += [(None if r is primary else primary["station_id"], r["station_id"]) for r in members]
    conn.executemany("UPDATE stations SET duplicate_of = ? WHERE station_id = ?", updates)
    return sum(1 for d, _ in updates if d)


def prune(conn: sqlite3.Connection, keep_days: int = RETENTION_DAYS):
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    conn.execute("DELETE FROM observations WHERE date < ?", (cutoff,))
    conn.execute("DELETE FROM hourly WHERE ts < ?", (cutoff,))
