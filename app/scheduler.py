"""Programación de la ingesta.

- Completa (31 días, todas las fuentes) dos veces al día: a primera hora y tras las 13:30, cuando
  InfoRiego publica el día anterior.
- Rápida (3 días) cada 3 horas para el día en curso. AEMET solo da las últimas 24 h de datos
  horarios, así que conviene no espaciarla más de ~12 h.
- Recuperación al arrancar y cada 30 min: las fuentes que llevan más de 3 h sin intentarse (PC apagado
  o suspendido, o la ventana se cerró a mitad de una descarga) se descargan con sus 31 días.

    python -m app.scheduler            # proceso dedicado (servicio 'worker' en Docker)
"""
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler

from app import db, ingest
from app.collectors import COLLECTORS

log = logging.getLogger("scheduler")
TZ = os.getenv("TZ_SCHEDULER", "Europe/Madrid")
_lock = threading.Lock()  # una ingesta cada vez: si coinciden dos trabajos, el segundo espera


def full():
    with _lock:
        log.info("Ingesta completa: %s", ingest.run(days=31))


def quick():
    with _lock:
        log.info("Ingesta rápida: %s", ingest.run(days=3))


def catch_up():
    with _lock:
        stale = _stale_sources()
        if stale:
            log.info("Recuperando fuentes desfasadas %s: %s", stale, ingest.run(stale, days=31))


def _configure(s):
    s.add_job(full, "cron", hour="6,14", minute=30, id="full", max_instances=1, coalesce=True)
    s.add_job(quick, "cron", hour="*/3", minute=5, id="quick", max_instances=1, coalesce=True)
    s.add_job(catch_up, "interval", minutes=30, id="catch_up", max_instances=1, coalesce=True,
              next_run_time=datetime.now(timezone.utc))  # también nada más arrancar
    return s


def _stale_sources(max_age_hours: float = 3) -> list[str]:
    """Fuentes (con sus claves puestas) que nunca se han intentado o no desde hace max_age_hours.
    Se mira cada una por separado: que AEMET esté al día no significa que el resto lo esté."""
    with db.session() as conn:
        attempts = dict(conn.execute("SELECT source, last_attempt FROM source_status").fetchall())
    limit = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    return [name for name, c in COLLECTORS.items()
            if all(os.getenv(k) for k in c.required_env)
            and (not attempts.get(name) or datetime.fromisoformat(attempts[name]) < limit)]


def start_background() -> BackgroundScheduler:
    s = _configure(BackgroundScheduler(timezone=TZ))
    s.start()
    return s


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = _configure(BlockingScheduler(timezone=TZ))
    log.info("Planificador en marcha (%s)", TZ)
    s.start()


if __name__ == "__main__":
    main()
