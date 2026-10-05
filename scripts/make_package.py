"""Empaqueta la app en un zip para llevarla a otro PC con Windows.

    python -m scripts.make_package                 # dist/RadarSetas.zip con claves y datos
    python -m scripts.make_package --sin-claves    # sin .env (en el otro PC se rellena a mano)
    python -m scripts.make_package --sin-datos     # sin la BD ni la caché (descarga todo de cero)

No incluye .venv (no es portable: el .bat lo crea en el otro equipo) ni .git.
"""
import argparse
import sqlite3
import subprocess
import tempfile
import zipfile
from pathlib import Path

from app.config import ROOT
from app.db import DB_PATH

NAME = "RadarSetas"


def code_files() -> list[Path]:
    """Ficheros del repo: versionados y nuevos no ignorados (respeta .gitignore)."""
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                         cwd=ROOT, capture_output=True, check=True).stdout.decode("utf-8")
    files = [ROOT / p for p in out.split("\0") if p]
    return [f for f in files if f.is_file() and not f.name.startswith(".git")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sin-claves", action="store_true", help="no incluir .env")
    ap.add_argument("--sin-datos", action="store_true", help="no incluir la BD ni la caché")
    ap.add_argument("--salida", type=Path, default=ROOT / "dist" / f"{NAME}.zip")
    args = ap.parse_args()

    args.salida.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.salida, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        def add(path: Path, arc: str | None = None):
            z.write(path, f"{NAME}/{arc or path.relative_to(ROOT).as_posix()}")

        for f in code_files():
            add(f)
        if not args.sin_claves and (ROOT / ".env").exists():
            add(ROOT / ".env")
        if not args.sin_datos:
            if DB_PATH.exists():
                # Copia consistente aunque la app esté abierta escribiendo en la BD
                with tempfile.TemporaryDirectory() as tmp:
                    copy = Path(tmp) / "rain.sqlite"
                    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(copy)
                    with dst:
                        src.backup(dst)
                    src.close()
                    dst.close()
                    add(copy, "data/rain.sqlite")
            for f in sorted((ROOT / "cache").rglob("*")):
                if f.is_file() and f.name != "ca-bundle.pem":  # se regenera en cada equipo
                    add(f)

    mb = args.salida.stat().st_size / 1e6
    print(f"Creado {args.salida} ({mb:.1f} MB)")
    if not args.sin_claves:
        print("Lleva tus claves (.env): no lo compartas con nadie más.")


if __name__ == "__main__":
    main()
