"""Lanzador de escritorio: web + actualización de datos en un solo proceso y abre el navegador.

    python -m app.launcher [--port 8000] [--no-browser]
"""
import argparse
import logging
import os
import socket
import threading
import time
import webbrowser
from urllib.request import urlopen

import uvicorn

log = logging.getLogger("launcher")


def _is_ours(port: int) -> bool:
    """¿Ya hay un Radar de lluvias escuchando en ese puerto?"""
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=2) as r:
            return b"max_window_days" in r.read()
    except OSError:
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
    if _is_ours(port):
        print(f"La app ya estaba abierta: http://localhost:{port}")
        if not args.no_browser:
            webbrowser.open(f"http://localhost:{port}")
        return
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
