"""Programación de la ingesta.

- Completa (31 días, todas las fuentes) dos veces al día: a primera hora y tras las 13:30, cuando
  InfoRiego publica el día anterior.
- Rápida (3 días) cada 3 horas para el día en curso. AEMET solo da las últimas 24 h de datos
  horarios, así que conviene no espaciarla más de ~12 h.

    python -m app.scheduler            # proceso dedicado (servicio 'worker' en Docker)
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler

from app import db, ingest

log = logging.getLogger("scheduler")
TZ = os.getenv("TZ_SCHEDULER", "Europe/Madrid")


def full():
    log.info("Ingesta completa: %s", ingest.run(days=31))


def quick():
    log.info("Ingesta rápida: %s", ingest.run(days=3))


def _configure(s):
    s.add_job(full, "cron", hour="6,14", minute=30, id="full", max_instances=1, coalesce=True)
    s.add_job(quick, "cron", hour="*/3", minute=5, id="quick", max_instances=1, coalesce=True)
    return s


def _needs_refresh(max_age_hours: float = 3) -> bool:
    """BD vacía o sin actualizar desde hace rato (p. ej. el PC ha estado apagado)."""
    with db.session() as conn:
        last = conn.execute("SELECT MAX(last_success) FROM source_status").fetchone()[0]
    if not last:
        return True
    return datetime.now(timezone.utc) - datetime.fromisoformat(last) > timedelta(hours=max_age_hours)


def start_background() -> BackgroundScheduler:
    s = _configure(BackgroundScheduler(timezone=TZ))
    if _needs_refresh():
        s.add_job(full, id="bootstrap")
    s.start()
    return s


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = _configure(BlockingScheduler(timezone=TZ))
    if _needs_refresh():
        s.add_job(full, id="bootstrap")
    log.info("Planificador en marcha (%s)", TZ)
    s.start()


if __name__ == "__main__":
    main()
