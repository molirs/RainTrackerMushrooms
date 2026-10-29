"""Punto de entrada de la app Android (Chaquopy). El WebView pide las mismas rutas /api/* que el
servidor y la app Kotlin se las pasa a handle(); la ingesta corre aquí, en hilos, sin servidor.

    mobile.init(files_dir, version)            # una vez al arrancar Python
    mobile.handle("GET", "/api/meta", "")      # -> (status, content_type, body)
    mobile.refresh()                           # lo llama también el WorkManager cada pocas horas

Las claves de API se guardan en <files_dir>/settings.json (las mete el usuario en la app).
"""
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote

log = logging.getLogger("mobile")

KEYS = ("AEMET_API_KEY", "SAIH_API_KEY", "SIAR_TOKEN")
FULL_EVERY = timedelta(hours=12)   # AEMET publica el diario con días de retraso: repasar los 31 días
QUICK_DAYS = 3
MIN_INTERVAL = timedelta(minutes=30)  # al abrir la app no se vuelve a pedir si es reciente

_base: Path | None = None
_version = ""
_lock = threading.Lock()
_progress: dict = {"running": False, "sources": {}, "started": None, "finished": None}


def init(files_dir: str, version: str = "") -> None:
    global _base, _version
    _base, _version = Path(files_dir), version
    os.environ["DB_PATH"] = str(_base / "rain.sqlite")
    os.environ["CACHE_DIR"] = str(_base / "cache")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _apply_settings(_load_json("settings.json"))


# ---- Ajustes y estado en disco ----------------------------------------------------------------
def _load_json(name: str) -> dict:
    try:
        return json.loads((_base / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_json(name: str, data: dict) -> None:
    path = _base / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def _apply_settings(settings: dict) -> None:
    from app import config
    for k in KEYS:
        v = (settings.get(k) or "").strip()
        os.environ[k] = v
        setattr(config, k, v)


def save_settings(values: dict) -> dict:
    settings = _load_json("settings.json")
    changed = []
    for k, v in values.items():
        if k in KEYS and (settings.get(k) or "") != v.strip():
            settings[k] = v.strip()
            changed.append(k)
    _save_json("settings.json", settings)
    _apply_settings(settings)
    if changed:  # una clave nueva: descargar ya sus 31 días
        from app.collectors import COLLECTORS
        sources = [c.source for c in COLLECTORS.values() if set(c.required_env) & set(changed)]
        refresh(force_full=sources)
    return settings_status()


def settings_status() -> dict:
    settings = _load_json("settings.json")
    return {k: bool(settings.get(k)) for k in KEYS}


# ---- Ingesta --------------------------------------------------------------------------------
def _plan(force_full: list[str]) -> dict[str, int]:
    """Días a descargar por fuente: 31 si toca repaso completo, si no los últimos QUICK_DAYS."""
    from app.collectors import COLLECTORS
    state = _load_json("refresh_state.json")
    now = datetime.now(timezone.utc)
    plan = {}
    for name in COLLECTORS:
        last_full = state.get(name, {}).get("last_full")
        full = name in force_full or not last_full or now - datetime.fromisoformat(last_full) > FULL_EVERY
        plan[name] = 31 if full else QUICK_DAYS
    return plan


def _run(plan: dict[str, int]) -> None:
    from app import ingest
    today = date.today()

    def one(name: str) -> None:
        _progress["sources"][name] = {"state": "running", "days": plan[name]}
        summary = ingest.run_source(name, today - timedelta(days=plan[name] - 1), today)
        failed = summary.startswith("ERROR")
        _progress["sources"][name] = {"state": "error" if failed else "skipped" if summary.startswith("omitida")
                                      else "done", "days": plan[name], "summary": summary}
        with _lock:
            state = _load_json("refresh_state.json")
            now = datetime.now(timezone.utc).isoformat()
            state.setdefault(name, {})["last_run"] = now
            if plan[name] == 31 and not failed and not summary.startswith("omitida"):
                state[name]["last_full"] = now
            _save_json("refresh_state.json", state)

    try:
        with ThreadPoolExecutor(max_workers=len(plan)) as pool:
            list(pool.map(one, plan))
        ingest.finish()
    except Exception:  # noqa: BLE001
        log.exception("Fallo en la actualización")
    finally:
        _progress["running"] = False
        _progress["finished"] = datetime.now(timezone.utc).isoformat()


def refresh(force_full: list[str] | None = None, wait: bool = False, if_older: bool = False) -> dict:
    """Lanza la actualización en segundo plano (o espera a que acabe, para el WorkManager).
    if_older: no hacer nada si todas las fuentes terminaron hace menos de MIN_INTERVAL."""
    with _lock:
        if not _progress["running"]:
            last = _oldest_run()
            if if_older and not force_full and last and \
                    datetime.now(timezone.utc) - datetime.fromisoformat(last) < MIN_INTERVAL:
                return progress()
            plan = _plan(force_full or [])
            _progress.update(running=True, started=datetime.now(timezone.utc).isoformat(), finished=None,
                             sources={n: {"state": "pending", "days": d} for n, d in plan.items()})
            threading.Thread(target=_run, args=(plan,), name="refresh", daemon=True).start()
    if wait:
        while _progress["running"]:
            threading.Event().wait(2)
    return progress()


def _oldest_run() -> str | None:
    """Cuándo terminó la fuente menos reciente; None si alguna no ha terminado nunca (p. ej. se
    cerró la app a mitad de la primera descarga)."""
    from app.collectors import COLLECTORS
    state = _load_json("refresh_state.json")
    runs = [state.get(name, {}).get("last_run") for name in COLLECTORS]
    return None if None in runs else min(runs)


def progress() -> dict:
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in _progress.items()}


# ---- Enrutado de /api/* ------------------------------------------------------------------------
def _q(query: dict, name: str, cast=str, default=None):
    v = query.get(name, [None])[0]
    if v is None or v == "":
        return default
    if cast is bool:
        return v.lower() in ("1", "true", "yes", "on")
    return cast(v)


def _route(method: str, path: str, q: dict):
    from app import api
    if method == "POST" and path == "/api/refresh":
        return refresh(force_full=api.csv(_q(q, "full")) or None)
    if method == "POST" and path == "/api/settings":
        return save_settings({k: q[k][0] for k in KEYS if k in q})
    if method != "GET":
        raise api.ApiError(405, "Método no permitido")
    if path == "/api/meta":
        return {**api.meta(), "mobile": True, "version": _version}
    if path == "/api/sources":
        return api.sources()
    if path == "/api/provinces":
        return ("application/geo+json", api.provinces_geojson())
    if path == "/api/stations":
        return api.stations(_q(q, "provinces"), _q(q, "sources"), _q(q, "alt_min", float), _q(q, "alt_max", float),
                            _q(q, "estimated", bool, False), _q(q, "duplicates", bool, False))
    if path in ("/api/rain", "/api/rain/daily"):
        common = (_q(q, "from", date.fromisoformat), _q(q, "to", date.fromisoformat), _q(q, "days", int),
                  _q(q, "provinces"), _q(q, "sources"), _q(q, "alt_min", float), _q(q, "alt_max", float))
        if path == "/api/rain":
            return api.rain(*common, _q(q, "min_mm", float, 0), _q(q, "estimated", bool, False))
        return api.rain_daily(*common, _q(q, "estimated", bool, True))
    if path.startswith("/api/stations/") and path.endswith("/series"):
        sid = unquote(path[len("/api/stations/"):-len("/series")])
        return api.station_series(sid, _q(q, "days", int, api.MAX_WINDOW_DAYS), _q(q, "to", date.fromisoformat))
    if path == "/api/refresh":
        return progress()
    if path == "/api/settings":
        return settings_status()
    raise api.ApiError(404, "Ruta no encontrada")


def handle(method: str, path: str, query: str = "") -> tuple[int, str, str]:
    from app import api
    try:
        out = _route(method.upper(), path, parse_qs(query or ""))
    except api.ApiError as e:
        return e.status, "application/json", json.dumps({"detail": e.detail})
    except ValueError as e:  # parámetro mal formado
        return 400, "application/json", json.dumps({"detail": str(e)})
    except Exception as e:  # noqa: BLE001
        log.exception("Error en %s %s", method, path)
        return 500, "application/json", json.dumps({"detail": f"{type(e).__name__}: {e}"})
    if isinstance(out, tuple):
        return 200, *out
    return 200, "application/json", json.dumps(out, ensure_ascii=False)
