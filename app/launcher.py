"""Lanzador de escritorio: web + actualización de datos en un solo proceso y abre el navegador.

    python -m app.launcher [--port 8000] [--no-browser]
"""
import argparse
import json
import logging
import os
import signal
import socket
import threading
import time
import webbrowser
from urllib.request import urlopen

import uvicorn

from app.config import source_build

log = logging.getLogger("launcher")


def _running_meta(port: int) -> dict | None:
    """/api/meta del Radar de lluvias que escucha en ese puerto, o None si no hay ninguno."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=2) as r:
            meta = json.loads(r.read())
        return meta if "max_window_days" in meta else None
    except (OSError, ValueError):
        return None


def _is_ours(port: int) -> bool:
    return _running_meta(port) is not None


def _stop_outdated(port: int, meta: dict) -> bool:
    """Cierra una instancia abierta con código anterior (p. ej. tras añadir una fuente)."""
    pid = meta.get("pid")
    if not pid:  # versiones sin 'pid' en /api/meta: no se puede cerrar desde aquí
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    for _ in range(40):
        if _port_free(port):
            return True
        time.sleep(0.25)
    return False


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _open_when_ready(url: str, port: int):
    for _ in range(60):
        if _is_ours(port):
            webbrowser.open(url)
            return
        time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    port = args.port
    meta = _running_meta(port)
    if meta and meta.get("build") == source_build():
        print(f"La app ya estaba abierta: http://localhost:{port}")
        if not args.no_browser:
            webbrowser.open(f"http://localhost:{port}")
        return
    if meta:
        print("Hay una versión anterior de la app abierta; la reinicio con la nueva...")
        if not _stop_outdated(port, meta):
            print(f"No he podido cerrarla: cierra la ventana antigua del Radar (puerto {port}).")
    while not _port_free(port):
        port += 1

    url = f"http://localhost:{port}"
    print("=" * 60)
    print(f"  Radar de lluvias para setas  ->  {url}")
    print("  Los datos se actualizan solos mientras esta ventana siga abierta.")
    print("  Cierra esta ventana (o pulsa Ctrl+C) para apagar la app.")
    print("=" * 60)

    os.environ["RUN_SCHEDULER"] = "1"
    if not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(url, port), daemon=True).start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
