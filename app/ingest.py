"""Ingesta: ejecuta los colectores y guarda en BD. Cada fuente es independiente: si una falla,
se registra el error y el resto continúa.

    python -m app.ingest                     # todas las fuentes, últimos 31 días
    python -m app.ingest --days 3 aemet      # refresco rápido de una fuente
"""
import argparse
import logging
from datetime import date, timedelta

from app import db
from app.collectors import COLLECTORS
from app.collectors.base import MissingCredentials

log = logging.getLogger("ingest")


def run_source(name: str, start: date, end: date) -> str:
    """Descarga y guarda una fuente. Nunca lanza: los errores quedan en source_status."""
    collector = COLLECTORS[name]()
    try:
        res = collector.fetch(start, end)
    except MissingCredentials as e:
        log.info("%s omitida: %s", name, e)
        return f"omitida: {e}"
    except Exception as e:  # noqa: BLE001
        log.exception("Fallo en %s", name)
        with db.session() as conn:
            db.record_status(conn, name, ok=False, error=f"{type(e).__name__}: {e}")
        return f"ERROR: {e}"
    with db.session() as conn:
        db.save_result(conn, res, day_window=collector.day_window or None)
        error = "; ".join(res.errors) or None
        db.record_status(conn, name, ok=True, error=error, n_stations=len(res.stations),
                         n_observations=len(res.observations) + len(res.hourly))
    summary = (f"{len(res.stations)} estaciones, {len(res.observations)} diarios, "
               f"{len(res.hourly)} horarios" + (f" ({len(res.errors)} avisos)" if res.errors else ""))
    log.info("%s: %s", name, summary)
    return summary


def finish() -> str:
    """Deduplicación entre redes y limpieza del histórico, tras ingerir una o varias fuentes."""
    with db.session() as conn:
        n_dup = db.deduplicate(conn)
        db.prune(conn)
    return f"{n_dup} estaciones marcadas como duplicadas"


def run(sources: list[str] | None = None, days: int = 31) -> dict[str, str]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    summary = {name: run_source(name, start, end) for name in sources or list(COLLECTORS)}
    summary["_dedup"] = finish()
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="*")
    ap.add_argument("--days", type=int, default=31)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for k, v in run(args.sources or None, args.days).items():
        print(f"{k:>12}: {v}")


if __name__ == "__main__":
    main()
