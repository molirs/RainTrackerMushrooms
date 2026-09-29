"""Paso 1 del plan: descarga N días de cada fuente y resume qué estaciones devuelve por provincia.

Uso:
    python -m scripts.probe_sources                 # todas las fuentes, 21 días
    python -m scripts.probe_sources meteocat aemet  # solo algunas
    python -m scripts.probe_sources --days 7 --csv  # guarda también data/probe_<fuente>.csv
"""
import argparse
import csv
import logging
import time
from collections import Counter, defaultdict
from datetime import date, timedelta

from app.collectors import COLLECTORS
from app.collectors.base import MissingCredentials
from app.config import ROOT
from app.geo import PROVINCES


def summarize(res, start, end):
    n_days = (end - start).days + 1
    by_prov = Counter(s.province or "¿?" for s in res.stations)
    with_data = {o.station_id for o in res.observations} | {h.station_id for h in res.hourly}
    prov_with_data = Counter(s.province or "¿?" for s in res.stations if s.station_id in with_data)
    days = Counter(o.date for o in res.observations)
    partial = sum(o.partial for o in res.observations)
    total_mm = defaultdict(float)
    for o in res.observations:
        total_mm[o.station_id] += o.precip_mm

    print(f"  Estaciones: {len(res.stations)} (con datos: {len(with_data)})  Horarios: {len(res.hourly)}  "
          f"Observaciones: {len(res.observations)} (parciales: {partial})")
    print("  Por provincia (estaciones / con datos):")
    for p in PROVINCES.values():
        print(f"    {p:<10} {by_prov.get(p, 0):>5} / {prov_with_data.get(p, 0):>5}")
    if by_prov.get("¿?"):
        print(f"    {'sin prov.':<10} {by_prov['¿?']:>5}")
    missing = [start + timedelta(days=i) for i in range(n_days) if (start + timedelta(days=i)) not in days]
    if days:
        print(f"  Fechas con datos: {min(days)} .. {max(days)}  "
              f"(sin datos: {', '.join(d.isoformat() for d in missing) or 'ninguna'})")
    if total_mm:
        by_id = {s.station_id: s for s in res.stations}
        top = sorted(total_mm.items(), key=lambda kv: -kv[1])[:5]
        print("  Más lluvia acumulada:")
        for sid, mm in top:
            s = by_id[sid]
            print(f"    {mm:7.1f} mm  {s.name} ({s.province}, {s.altitude} m)")
    for e in res.errors:
        print(f"  ! {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="*", default=list(COLLECTORS))
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--csv", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    end = date.today()
    start = end - timedelta(days=args.days - 1)
    print(f"Periodo: {start} .. {end}\n")

    for name in args.sources:
        collector = COLLECTORS[name]()
        print(f"== {collector.label} ({name})")
        t0 = time.time()
        try:
            res = collector.fetch(start, end)
        except MissingCredentials as e:
            print(f"  (omitido) {e}\n")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR: {type(e).__name__}: {e}\n")
            continue
        summarize(res, start, end)
        print(f"  Tiempo: {time.time() - t0:.1f} s\n")
        if args.csv:
            out = ROOT / "data" / f"probe_{name}.csv"
            out.parent.mkdir(exist_ok=True)
            rows = list(res.rows())
            if rows:
                with out.open("w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=rows[0].keys())
                    w.writeheader()
                    w.writerows(rows)
                print(f"  CSV: {out}\n")


if __name__ == "__main__":
    main()
