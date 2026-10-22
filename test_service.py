"""Backend tests. No network: sources are fed sample payloads in each publisher's format."""
import json
import os
import sys
import threading
from http.server import HTTPServer
from urllib.request import Request, urlopen

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agsi_service import core, service, sources  # noqa: E402

GLD_CSV = """SPDR GOLD SHARES
Date,GLD Close,LBMA Gold Price,NAV per GLD in Gold,NAV/share at 10.30 a.m. NYT,Indicative Price of GLD at 4.15 p.m. NYT,Mid point of bid/ask spread at 4.15 p.m. NYT#,Premium/Discount of GLD mid point v Indicative Value of GLD at 4.15 p.m. NYT,Daily Share Volume,Total Net Asset Value Ounces in the Trust as at 4.15 p.m. NYT,Total Net Asset Value Tonnes in the Trust as at 4.15 p.m. NYT,Total Net Asset Value in the Trust
09-Oct-2026,250.10,2600.00,0.0962,250.00,250.05,250.06,0.01%,"8,123,456","30,500,000.000",948.65,"$76,000,000,000"
12-Oct-2026, HOLIDAY,,,,,,,,,,
13-Oct-2026,251.00,2610.00,0.0962,251.00,251.02,251.03,0.00%,"7,000,000","30,400,000.000",945.54,"$76,100,000,000"
16-Oct-2026,249.00,2590.00,0.0962,249.00,249.01,249.02,0.00%,"9,000,000","30,200,000.000",939.32,"$75,500,000,000"
20-Oct-2026,248.00,2580.00,0.0962,248.00,248.01,248.02,0.00%,"9,500,000","30,100,000.000",936.21,"$75,000,000,000"
"""

CFTC_ROWS = [{"market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.", "report_date_as_yyyy_mm_dd": "2026-10-13T00:00:00.000",
              "cftc_contract_market_code": "088691", "open_interest_all": "512345"}]

CME_ROWS = [["", "COMMODITY EXCHANGE, INC.", "", "", ""],
            ["", "METAL DEPOSITORY STATISTICS", "", "", ""],
            ["", "Report Date: 10/20/2026", "", "", ""],
            ["", "Activity Date: 10/19/2026", "", "", ""],
            ["DEPOSITORY", "", "PREV TOTAL", "RECEIVED", "TOTAL TODAY"],
            ["BRINK'S, INC.", "Registered", 1000.0, 0.0, 1000.0],
            ["", "TOTAL REGISTERED", 19_100_000.0, 2_000.0, 19_250_400.5],
            ["", "TOTAL ELIGIBLE", 15_000_000.0, 0.0, 15_100_000.0]]


def fred(values, start="2026-10-20"):
    return [(start, v) for v in values]


# ------------------------------------------------------------ parsers
def test_parse_cftc():
    r = sources.parse_cftc(CFTC_ROWS)
    assert r["value"] == 512.345 and r["as_of"] == "2026-10-13" and r["status"] == "ok"


def test_parse_cftc_empty():
    with pytest.raises(ValueError):
        sources.parse_cftc([])


def test_parse_gld_week_change_skips_holiday():
    r = sources.parse_gld_csv(GLD_CSV)
    # latest 20-Oct 936.21 vs last row on/before 13-Oct (945.54) -> -9.33 t (outflow)
    assert r["value"] == -9.33 and r["as_of"] == "2026-10-20" and r["detail"]["compared_with"] == "2026-10-13"


def test_parse_gld_bad_header():
    with pytest.raises(ValueError):
        sources.parse_gld_csv("foo,bar\n1,2\n")


def test_parse_cme_rows():
    r = sources.parse_cme_rows(CME_ROWS)
    assert r["value"] == 19.2504 and r["as_of"] == "2026-10-20"


def test_parse_cme_missing_total():
    with pytest.raises(ValueError):
        sources.parse_cme_rows(CME_ROWS[:5])


def test_parse_macro_formula():
    fsi = [("2026-10-17", 0.8)]
    ry = [("2026-10-20", 2.10)] + [("2026-10-01", 2.00)] * 19 + [("2026-09-22", 1.85)] + [("x", 1.0)] * 5
    r = sources.parse_macro(fsi, ry)
    # fsi part 50+25*0.8 = 70; shock |2.10-1.85| = 0.25 -> 50; average 60
    assert r["value"] == 60.0 and r["as_of"] == "2026-10-20" and r["detail"]["dfii10_change_20d"] == 0.25


def test_parse_macro_clamps():
    r = sources.parse_macro([("d", 9.0)], [("d", 5.0)] + [("d", 0.0)] * 20)
    assert r["value"] == 100.0


def test_macro_without_key(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    r = sources.macro_stress()
    assert r["status"] == "unavailable" and "FRED_API_KEY" in r["reason"]


def test_fetch_failure_is_reported_not_raised(monkeypatch):
    def boom(*a, **k):
        raise sources.requests.ConnectionError("down")
    monkeypatch.setattr(sources.requests, "get", boom)
    for fn in (sources.open_interest, sources.etf_flow, sources.registered_inventory):
        r = fn()
        assert r["status"] == "unavailable" and r["value"] is None and "could not be reached" in r["reason"]


# ------------------------------------------------------------ endpoint body
def ok(v, d="2026-10-20"):
    return lambda: {"value": v, "as_of": d, "source": "test", "status": "ok"}


FULL = {"registered_inventory": ok(9), "open_interest": ok(620), "etf_flow": ok(-12),
        "delivery_ratio": ok(14), "macro_stress": ok(55)}


def test_build_complete_matches_known_score():
    b = service.build_latest(FULL)
    assert b["complete"] and b["agsi"]["score"] == 71.23 and b["agsi"]["heat"] == "Rising Risk"
    assert b["model"] == core.MODEL_VERSION and set(b["components"]) == set(core.WEIGHTS)


def test_build_partial_has_components_but_no_score():
    f = {**FULL, "delivery_ratio": sources.delivery_ratio}
    b = service.build_latest(f)
    assert not b["complete"] and b["agsi"] is None and "delivery" not in b["components"]
    assert b["inputs"]["delivery_ratio"]["status"] == "unavailable" and b["inputs"]["delivery_ratio"]["value"] is None
    assert b["components"]["paper"] == 80.0


def test_out_of_range_value_is_rejected():
    b = service.build_latest({**FULL, "open_interest": ok(99999)})
    assert b["inputs"]["open_interest"]["value"] is None and "outside" in b["inputs"]["open_interest"]["reason"]


def test_crashing_fetcher_does_not_break_response():
    def bad():
        return {"value": float("nan"), "status": "ok", "source": "x", "as_of": None}
    b = service.build_latest({**FULL, "etf_flow": bad})
    assert b["inputs"]["etf_flow"]["status"] == "unavailable"


def test_cors_headers(monkeypatch):
    monkeypatch.setenv("AGSI_ALLOWED_ORIGINS", "https://a.example,https://b.example")
    assert service.headers("https://b.example")["Access-Control-Allow-Origin"] == "https://b.example"
    assert service.headers("https://evil.example")["Access-Control-Allow-Origin"] == "https://a.example"
    monkeypatch.setenv("AGSI_ALLOWED_ORIGINS", "*")
    assert service.headers(None)["Access-Control-Allow-Origin"] == "*"


def test_vercel_handler_serves_json(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("agsi_latest", os.path.join(os.path.dirname(__file__), "..", "api", "agsi-latest.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(service.sources, "FETCHERS", FULL)
    service._cache.update(at=0, body=None)
    srv = HTTPServer(("127.0.0.1", 0), mod.handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    with urlopen(Request(f"http://127.0.0.1:{srv.server_port}/api/agsi-latest")) as r:
        body = json.loads(r.read())
        assert r.status == 200 and "s-maxage" in r.headers["Cache-Control"]
    assert body["agsi"]["score"] == 71.23
    srv.server_close()
