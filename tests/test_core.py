from datetime import date, datetime, timezone

import pytest

from app import db
from app.collectors.aemet import parse_dms as aemet_dms
from app.collectors.base import CollectorResult, HourlyObservation, Observation, Station, to_float
from app.collectors.inforiego import parse_dms as inforiego_dms
from app.geo import normalize_province, province_at, utm_to_latlon


def test_to_float():
    assert to_float("1,2") == 1.2
    assert to_float("Ip") == 0.0
    assert to_float("Acum") is None
    assert to_float("") is None


def test_dms_parsers():
    assert aemet_dms("413800N") == pytest.approx(41.6333, abs=1e-4)
    assert aemet_dms("010340W") == pytest.approx(-1.0611, abs=1e-4)
    assert inforiego_dms("022912000W") == pytest.approx(-2.4867, abs=1e-4)
    assert inforiego_dms("412639000N") == pytest.approx(41.4442, abs=1e-4)


def test_utm_to_latlon():
    lat, lon = utm_to_latlon(605801.48688, 4673641.9007, zone=30)  # Cadreita INTIA (IDENA)
    assert lat == pytest.approx(42.2077854, abs=1e-6)
    assert lon == pytest.approx(-1.71830861, abs=1e-6)


def test_provinces():
    assert normalize_province("LERIDA") == "Lleida"
    assert normalize_province("Rioja, La") == "La Rioja"
    assert normalize_province("Madrid") is None
    assert province_at(41.65, -0.88) == "Zaragoza"
    assert province_at(42.82, -1.64) == "Navarra"
    assert province_at(40.42, -3.70) is None  # Madrid


def _station(sid, source, lat=42.0, lon=-1.0):
    return Station(sid, source, sid, lat, lon, 500.0, "Navarra")


def test_partial_never_overwrites_final(tmp_path):
    d = date(2026, 9, 1)
    with db.session(tmp_path / "t.sqlite") as conn:
        res = CollectorResult("x", [_station("x:1", "x")], [Observation("x:1", "x", d, 10.0)])
        db.save_result(conn, res)
        db.save_result(conn, CollectorResult("x", [], [Observation("x:1", "x", d, 3.0, partial=True)]))
        assert tuple(conn.execute("SELECT precip_mm, partial FROM observations").fetchone()) == (10.0, 0)
        db.save_result(conn, CollectorResult("x", [], [Observation("x:1", "x", d, 12.0)]))
        assert conn.execute("SELECT precip_mm FROM observations").fetchone()[0] == 12.0


def test_hourly_accumulates_across_runs(tmp_path):
    def h(hour, mm, day=2):
        return HourlyObservation("a:1", "a", datetime(2026, 9, day, hour, tzinfo=timezone.utc), mm)

    with db.session(tmp_path / "t.sqlite") as conn:
        db.save_result(conn, CollectorResult("a", [_station("a:1", "a")], hourly=[h(1, 1.0), h(2, 2.0)]))
        # Segunda ejecución: ventana deslizante, ya no trae la 01:00 pero sí horas nuevas
        db.save_result(conn, CollectorResult("a", [], hourly=[h(2, 2.0), h(3, 0.5), h(0, 4.0, day=3)]))
        rows = dict(conn.execute("SELECT date, precip_mm FROM observations").fetchall())
        assert rows == {"2026-09-02": 7.5}  # la hora que acaba a las 00:00 del 3 es del día 2
        db.save_result(conn, CollectorResult("a", [], [Observation("a:1", "a", date(2026, 9, 2), 8.0)]))
        db.save_result(conn, CollectorResult("a", [], hourly=[h(4, 1.0)]))
        assert tuple(conn.execute("SELECT precip_mm, partial FROM observations").fetchone()) == (8.0, 0)


def test_deduplicate_prefers_original_source(tmp_path):
    with db.session(tmp_path / "t.sqlite") as conn:
        db.save_result(conn, CollectorResult("aemet", [_station("aemet:1", "aemet", 42.0, -1.0)]))
        db.save_result(conn, CollectorResult("meteonavarra", [
            _station("meteonavarra:9", "meteonavarra", 42.001, -1.001),   # ~140 m
            _station("meteonavarra:8", "meteonavarra", 42.05, -1.0),      # ~5,5 km
        ]))
        assert db.deduplicate(conn) == 1
        dup = dict(conn.execute("SELECT station_id, duplicate_of FROM stations").fetchall())
        assert dup == {"aemet:1": None, "meteonavarra:9": "aemet:1", "meteonavarra:8": None}


def test_hourly_aemet_window_07_07(tmp_path):
    """AEMET: el día D va de D 07:00 a D+1 07:00 UTC, igual que su climatología diaria."""
    def h(day, hour, mm):
        return HourlyObservation("aemet:1", "aemet", datetime(2026, 9, day, hour, tzinfo=timezone.utc), mm)

    with db.session(tmp_path / "t.sqlite") as conn:
        res = CollectorResult("aemet", [_station("aemet:1", "aemet")],
                              hourly=[h(2, 7, 1.0), h(2, 8, 2.0), h(3, 3, 4.0), h(3, 7, 0.5), h(3, 8, 9.0)],
                              hourly_day_start_utc=7)
        db.save_result(conn, res)
        rows = dict(conn.execute("SELECT date, precip_mm FROM observations").fetchall())
        # 06-07h del día 2 -> día 1 ; 07h del 2 a 07h del 3 -> día 2 ; 07-08h del 3 -> día 3
        assert rows == {"2026-09-01": 1.0, "2026-09-02": 6.5, "2026-09-03": 9.0}


def test_rebuild_drops_stale_partial_days(tmp_path):
    """Si cambia la ventana de agregación, no quedan días parciales huérfanos de la anterior."""
    with db.session(tmp_path / "t.sqlite") as conn:
        h = HourlyObservation("a:1", "a", datetime(2026, 9, 2, 3, tzinfo=timezone.utc), 2.0)
        db.save_result(conn, CollectorResult("a", [_station("a:1", "a")], hourly=[h]))
        db.save_result(conn, CollectorResult("a", [], hourly=[h], hourly_day_start_utc=7))
        assert dict(conn.execute("SELECT date, precip_mm FROM observations").fetchall()) == {"2026-09-01": 2.0}


def _st(sid, source, lat, lon, alt, keys=()):
    return Station(sid, source, sid, lat, lon, alt, "Navarra", match_keys=keys)


def test_dedup_requires_similar_altitude(tmp_path):
    with db.session(tmp_path / "t.sqlite") as conn:
        db.save_result(conn, CollectorResult("aemet", [_st("aemet:1", "aemet", 42.0, -1.0, 500)]))
        # A 100 m pero 80 m más alta: otro instrumento (p. ej. en la ladera), no se fusiona
        db.save_result(conn, CollectorResult("meteonavarra", [_st("meteonavarra:1", "meteonavarra", 42.0009, -1.0, 580)]))
        assert db.deduplicate(conn) == 0


def test_dedup_by_match_key_beyond_radius(tmp_path):
    with db.session(tmp_path / "t.sqlite") as conn:
        db.save_result(conn, CollectorResult("inforiego", [
            _st("inforiego:SO03", "inforiego", 41.8350, -2.4314, 1011, ("siar-code:SO03",))]))
        db.save_result(conn, CollectorResult("siar", [
            _st("siar:SO03", "siar", 41.8386, -2.4314, 1019, ("siar-code:SO03",)),       # ~400 m (ED50)
            _st("siar:SO09", "siar", 41.9000, -2.4314, 1019, ("siar-code:SO09",))]))
        assert db.deduplicate(conn) == 1
        dup = dict(conn.execute("SELECT station_id, duplicate_of FROM stations").fetchall())
        assert dup["siar:SO03"] == "inforiego:SO03" and dup["siar:SO09"] is None


def test_effective_observations_fill_gaps(tmp_path):
    d1, d2, d3 = date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)
    with db.session(tmp_path / "t.sqlite") as conn:
        db.save_result(conn, CollectorResult("aemet", [_st("aemet:1", "aemet", 42.0, -1.0, 500)], [
            Observation("aemet:1", "aemet", d1, 5.0),
            Observation("aemet:1", "aemet", d3, 1.0, partial=True)]))
        db.save_result(conn, CollectorResult("meteonavarra", [_st("meteonavarra:1", "meteonavarra", 42.0, -1.0, 505)], [
            Observation("meteonavarra:1", "meteonavarra", d1, 7.0),   # la principal ya tiene dato: se ignora
            Observation("meteonavarra:1", "meteonavarra", d2, 3.0),   # hueco en la principal: se rellena
            Observation("meteonavarra:1", "meteonavarra", d3, 4.0)])) # definitivo gana a parcial
        db.deduplicate(conn)
        rows = {r["date"]: (r["precip_mm"], r["from_source"]) for r in conn.execute(
            "SELECT * FROM effective_observations WHERE station_id = 'aemet:1'")}
        assert rows == {"2026-09-01": (5.0, "aemet"), "2026-09-02": (3.0, "meteonavarra"),
                        "2026-09-03": (4.0, "meteonavarra")}
        assert conn.execute("SELECT COUNT(*) FROM effective_observations WHERE station_id = 'meteonavarra:1'"
                            ).fetchone()[0] == 0
