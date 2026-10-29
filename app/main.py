"""API REST + frontend estático. La lógica está en app/api.py (la comparte la app Android).

    uvicorn app.main:app --reload
"""
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app import api, db
from app.api import MAX_WINDOW_DAYS
from app.config import source_build

STATIC = Path(__file__).parent / "static"
BUILD = source_build()


@asynccontextmanager
async def lifespan(_app):
    with db.session():
        pass  # crea el esquema si no existe
    scheduler = None
    if os.getenv("RUN_SCHEDULER") == "1":  # despliegue en un solo proceso
        from app.scheduler import start_background
        scheduler = start_background()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Radar de lluvias para setas", version="0.1", lifespan=lifespan)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except api.ApiError as e:
        raise HTTPException(e.status, e.detail) from None


@app.get("/api/sources")
def sources():
    return api.sources()


@app.get("/api/provinces")
def provinces():
    return Response(api.provinces_geojson(), media_type="application/geo+json",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/stations")
def stations(provinces: str | None = None, sources: str | None = None,
             alt_min: float | None = None, alt_max: float | None = None,
             estimated: bool = False, duplicates: bool = False):
    return api.stations(provinces, sources, alt_min, alt_max, estimated, duplicates)


@app.get("/api/rain")
def rain(from_: date | None = Query(None, alias="from"), to: date | None = None,
         days: int | None = Query(None, ge=1, le=MAX_WINDOW_DAYS),
         provinces: str | None = None, sources: str | None = None,
         alt_min: float | None = None, alt_max: float | None = None,
         min_mm: float = 0, estimated: bool = False):
    """Lluvia acumulada por estación entre from y to (ambos incluidos)."""
    return _call(api.rain, from_, to, days, provinces, sources, alt_min, alt_max, min_mm, estimated)


@app.get("/api/rain/daily")
def rain_daily(from_: date | None = Query(None, alias="from"), to: date | None = None,
               days: int | None = Query(None, ge=1, le=MAX_WINDOW_DAYS),
               provinces: str | None = None, sources: str | None = None,
               alt_min: float | None = None, alt_max: float | None = None, estimated: bool = True):
    """Matriz estación × día (mm, null = sin dato) para animar y acumular en el cliente."""
    return _call(api.rain_daily, from_, to, days, provinces, sources, alt_min, alt_max, estimated)


@app.get("/api/stations/{station_id}/series")
def station_series(station_id: str, days: int = Query(MAX_WINDOW_DAYS, ge=1, le=MAX_WINDOW_DAYS), to: date | None = None):
    return _call(api.station_series, station_id, days, to)


@app.get("/api/meta")
def meta():
    return {**api.meta(), "build": BUILD, "pid": os.getpid()}


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def revalidate_static(request, call_next):
        # Sin esto el navegador sigue usando el app.js antiguo tras actualizar la app
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")
