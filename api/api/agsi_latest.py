"""AURUM data service: GET /api/agsi-latest   (Vercel Python function)

Self-contained on purpose: it uses only Python's standard library, so Vercel has nothing to
install and no other project folder to find. The one optional extra, xlrd (for the CME .xls
report), is imported only when needed. Without it, that single figure is reported unavailable.

It never answers with a server error. If a source fails, that input comes back with
"status": "unavailable", a reason and a clearly labelled "sample_value". If everything fails,
the response still has the same shape, with "fallback": true. Real data only ever appears in
"value"; sample numbers only ever appear in "sample_value".

Environment variables: FRED_API_KEY (needed for macro stress), CFTC_APP_TOKEN (optional),
AGSI_ALLOWED_ORIGINS (default *), AGSI_CACHE_SECONDS (default 900), AGSI_HTTP_TIMEOUT (default 8).
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))   # harmless; keeps local imports findable

import csv
import json
import logging
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger("agsi")

# =============================================================== model agsi-1.1.0
# Same maths as the AURUM app (aurum/engine.py) and page; the app's tests check they agree.
MODEL_VERSION = "agsi-1.1.0"
WEIGHTS = {"physical": 0.25, "paper": 0.25, "etf": 0.20, "delivery": 0.20, "macro": 0.10}
# key: (component, valid_min, valid_max, norm_low, norm_high, higher_is_stress, unit)
INPUTS = {
    "registered_inventory": ("physical", 0.0, 100.0, 5.0, 25.0, False, "million troy oz"),
    "open_interest":        ("paper",    0.0, 5000.0, 300.0, 700.0, True, "thousand contracts"),
    "etf_flow":             ("etf",   -500.0, 500.0, -50.0, 50.0, False, "tonnes"),
    "delivery_ratio":       ("delivery", 0.0, 100.0, 2.0, 20.0, True, "% of open interest"),
    "macro_stress":         ("macro",    0.0, 100.0, 0.0, 100.0, True, "index 0-100"),
}
BANDS = [(0.0, "Calm", "Calm accumulation phase"), (30.0, "Watch", "Transition phase"),
         (60.0, "Rising Risk", "Liquidity stress phase"), (80.0, "System Stress", "Crisis regime")]
# Ranges used only for labelled sample values (illustrative, not market data).
SAMPLE_RANGES = {"registered_inventory": (14, 24, 2), "open_interest": (420, 580, 1), "etf_flow": (-35, 35, 1),
                 "delivery_ratio": (2.5, 12, 2), "macro_stress": (25, 65, 0)}


def round_half_up(x, places):
    f = 10 ** places
    return math.floor(x * f + 0.5) / f


def normalize(value, low, high, higher_is_stress=True):
    if high == low:
        return 50.0
    frac = min(1.0, max(0.0, (value - low) / (high - low)))
    if not higher_is_stress:
        frac = 1.0 - frac
    return round_half_up(frac * 100.0, 4)


def classify(score):
    score = float(score)
    if not math.isfinite(score):
        raise ValueError("score must be finite")
    band = BANDS[-1]
    for b, nxt in zip(BANDS, BANDS[1:]):
        if score < nxt[0]:
            band = b
            break
    return {"heat": band[1], "regime": band[2]}


def valid(key, value):
    _, lo, hi, *_ = INPUTS[key]
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and lo <= value <= hi


def components(values):
    out = {}
    for key, (comp, _, _, low, high, up, _) in INPUTS.items():
        v = values.get(key)
        if v is not None and valid(key, v):
            out[comp] = normalize(v, low, high, up)
    return out


def score(values):
    comp = components(values)
    if len(comp) != len(INPUTS):
        return None
    contrib = {c: round_half_up(comp[c] * WEIGHTS[c], 4) for c in WEIGHTS}
    total = 0.0
    for c in WEIGHTS:
        total += contrib[c]
    s = round_half_up(min(100.0, max(0.0, total)), 2)
    return {"score": s, "contributions": contrib, **classify(s)}


def sample_value(key):
    lo, hi, dp = SAMPLE_RANGES[key]
    return round_half_up(lo + random.random() * (hi - lo), dp)


# =============================================================== HTTP helper (stdlib only)
TIMEOUT = float(os.getenv("AGSI_HTTP_TIMEOUT", "8") or 8)
UA = "AURUM-Intelligence/1.1 (research data service)"


class FetchError(Exception):
    pass


def http_get(url, params=None, headers=None):
    if params:
        url = url + ("&" if "?" in url else "?") + urlencode(params)
    req = Request(url, headers={"User-Agent": UA, "Accept": "*/*", **(headers or {})})
    try:
        with urlopen(req, timeout=TIMEOUT) as r:
            return r.read(8 * 1024 * 1024)
    except HTTPError as e:
        raise FetchError(f"the source answered with HTTP {e.code}")
    except (URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        if "timed out" in str(reason).lower():
            raise FetchError("the source took too long to answer")
        raise FetchError("the source could not be reached")


def _ok(value, as_of, source, **detail):
    return {"value": value, "as_of": as_of, "source": source, "status": "ok", "detail": detail}


def _na(source, reason):
    return {"value": None, "as_of": None, "source": source, "status": "unavailable", "reason": reason}


def _why(e):
    return str(e)[:160] if isinstance(e, (FetchError, ValueError)) else f"unexpected error ({e.__class__.__name__})"


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _num(text):
    try:
        return float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_date(text):
    text = (text or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# =============================================================== sources
CFTC_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"     # Legacy COT, futures only
CFTC_GOLD_CODE = "088691"                                                # Gold, COMEX
GLD_URL = os.getenv("AGSI_GLD_CSV_URL", "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv")
CME_STOCKS_URL = os.getenv("AGSI_CME_STOCKS_URL", "https://www.cmegroup.com/delivery_reports/Gold_Stocks.xls")
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

SRC_CFTC = "CFTC Commitments of Traders (legacy, futures only): Gold, COMEX"
SRC_GLD = "SPDR Gold Shares (GLD) published holdings, 7-day change"
SRC_CME = "CME Group COMEX gold depository report (Gold_Stocks.xls)"
SRC_DELIVERY = "CME Group delivery notices"
SRC_FRED = "FRED: STLFSI4 financial stress index and DFII10 10-year real yield"
MACRO_METHOD = ("macro_stress = 0.5 x clamp(50 + 25 x STLFSI4, 0, 100) "
                "+ 0.5 x clamp(|20-day change in DFII10| / 0.50 x 100, 0, 100)")


def parse_cftc(rows):
    if not rows:
        raise ValueError("no gold rows in the CFTC response")
    row = rows[0]
    oi = float(row["open_interest_all"])
    return _ok(round(oi / 1000.0, 3), str(row["report_date_as_yyyy_mm_dd"])[:10], SRC_CFTC,
               contracts=int(oi), market=row.get("market_and_exchange_names"))


def open_interest():
    try:
        token = os.getenv("CFTC_APP_TOKEN", "")
        body = http_get(CFTC_URL, {"cftc_contract_market_code": CFTC_GOLD_CODE,
                                   "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": 1},
                        {"X-App-Token": token} if token else None)
        return parse_cftc(json.loads(body))
    except Exception as e:
        log.warning("CFTC failed: %s", e)
        return _na(SRC_CFTC, _why(e))


def parse_gld_csv(text, days=7):
    lines = text.splitlines()
    hdr_i = next((i for i, l in enumerate(lines[:30]) if "date" in l.lower() and "tonne" in l.lower()), None)
    if hdr_i is None:
        raise ValueError("could not find the Date / Tonnes columns in the GLD file")
    reader = csv.reader(lines[hdr_i:])
    header = [h.strip().lower() for h in next(reader)]
    i_date = next(i for i, h in enumerate(header) if h.startswith("date"))
    i_t = next(i for i, h in enumerate(header) if "tonne" in h)
    series = []
    for row in reader:
        if len(row) <= max(i_date, i_t):
            continue
        d, t = _parse_date(row[i_date]), _num(row[i_t])
        if d and t is not None and t > 0:
            series.append((d, t))
    if len(series) < 2:
        raise ValueError("not enough GLD holdings rows")
    series.sort()
    latest_d, latest_t = series[-1]
    prior = [x for x in series if x[0] <= latest_d - timedelta(days=days)]
    if not prior:
        raise ValueError(f"no GLD holdings from {days} days before the latest row")
    prior_d, prior_t = prior[-1]
    return _ok(round(latest_t - prior_t, 2), latest_d.isoformat(), SRC_GLD,
               tonnes_now=latest_t, tonnes_before=prior_t, compared_with=prior_d.isoformat(),
               note="GLD only; other gold ETFs are not included")


def etf_flow():
    try:
        return parse_gld_csv(http_get(GLD_URL).decode("utf-8-sig", errors="replace"))
    except Exception as e:
        log.warning("GLD failed: %s", e)
        return _na(SRC_GLD, _why(e))


_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def parse_cme_rows(rows):
    as_of = None
    for row in rows[:15]:
        for cell in row:
            m = _DATE_RE.search(str(cell))
            if m and as_of is None:
                mo, dy, yr = map(int, m.groups())
                try:
                    as_of = date(yr, mo, dy).isoformat()
                except ValueError:
                    pass
    for row in rows:
        label = " ".join(str(c) for c in row if isinstance(c, str)).upper()
        if "TOTAL REGISTERED" in label:
            nums = [c for c in row if isinstance(c, (int, float)) and not isinstance(c, bool)]
            if nums:
                oz = float(nums[-1])
                return _ok(round(oz / 1e6, 4), as_of, SRC_CME, troy_oz=oz)
    raise ValueError("no 'TOTAL REGISTERED' row in the CME report")


def registered_inventory():
    try:
        import xlrd   # optional dependency; listed in requirements.txt
    except Exception:
        return _na(SRC_CME, "the xlrd package is not installed on the server (see requirements.txt)")
    try:
        book = xlrd.open_workbook(file_contents=http_get(CME_STOCKS_URL))
        sh = book.sheet_by_index(0)
        return parse_cme_rows([sh.row_values(i) for i in range(sh.nrows)])
    except Exception as e:
        log.warning("CME stocks failed: %s", e)
        return _na(SRC_CME, _why(e) if isinstance(e, (FetchError, ValueError)) else "the CME report could not be read")


def delivery_ratio():
    return _na(SRC_DELIVERY, "CME publishes delivery notices only as PDF reports, which this version does not read automatically")


def _fred(series_id, key, limit):
    data = json.loads(http_get(FRED_URL, {"series_id": series_id, "api_key": key, "file_type": "json",
                                          "sort_order": "desc", "limit": limit}))
    obs = [(o["date"], float(o["value"])) for o in data.get("observations", []) if o.get("value") not in (".", "", None)]
    if not obs:
        raise ValueError(f"FRED returned no data for {series_id}")
    return obs


def parse_macro(fsi_obs, ry_obs):
    fsi_date, fsi = fsi_obs[0]
    if len(ry_obs) < 21:
        raise ValueError("not enough DFII10 history for a 20-day change")
    ry_date, ry = ry_obs[0]
    ry_change = ry - ry_obs[20][1]
    fsi_part = _clamp(50 + 25 * fsi)
    shock_part = _clamp(abs(ry_change) / 0.50 * 100)
    value = round(0.5 * fsi_part + 0.5 * shock_part, 1)
    return _ok(value, max(fsi_date, ry_date), SRC_FRED, stlfsi4=fsi, stlfsi4_date=fsi_date,
               dfii10=ry, dfii10_date=ry_date, dfii10_change_20d=round(ry_change, 3),
               fsi_part=round(fsi_part, 1), real_yield_shock_part=round(shock_part, 1), method=MACRO_METHOD)


def macro_stress():
    key = os.getenv("FRED_API_KEY", "").strip()
    if not key:
        return _na(SRC_FRED, "FRED_API_KEY is not set on the server")
    try:
        return parse_macro(_fred("STLFSI4", key, 4), _fred("DFII10", key, 40))
    except Exception as e:
        log.warning("FRED failed: %s", e)
        return _na(SRC_FRED, _why(e))


FETCHERS = {"registered_inventory": registered_inventory, "open_interest": open_interest,
            "etf_flow": etf_flow, "delivery_ratio": delivery_ratio, "macro_stress": macro_stress}


# =============================================================== response
DISCLAIMER = ("Research data for educational and analytical use only. "
              "Not financial, investment, legal or trading advice.")


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe(fn, key):
    try:
        r = fn()
        return r if isinstance(r, dict) else _na(None, "the source returned nothing usable")
    except Exception as e:      # a fetcher should never raise, but never let one break the reply
        log.exception("fetcher %s crashed", key)
        return _na(None, f"unexpected error ({e.__class__.__name__})")


def build_latest(fetchers=None):
    fetchers = fetchers or FETCHERS
    with ThreadPoolExecutor(max_workers=max(1, len(fetchers))) as pool:
        futures = {k: pool.submit(_safe, fn, k) for k, fn in fetchers.items()}
        raw = {k: f.result() for k, f in futures.items()}

    inputs, values, warnings = {}, {}, []
    for key, (_c, vmin, vmax, _lo, _hi, _up, unit) in INPUTS.items():
        r = raw.get(key) or _na(None, "not fetched")
        v = r.get("value")
        if r.get("status") == "ok" and not valid(key, v):
            r = {**r, "value": None, "status": "unavailable",
                 "reason": f"value {v!r} is outside the accepted range {vmin:g} to {vmax:g}"}
        entry = {"value": r.get("value"), "unit": unit, "as_of": r.get("as_of"), "source": r.get("source"),
                 "status": "ok" if r.get("status") == "ok" else "unavailable"}
        if entry["status"] == "ok":
            values[key] = entry["value"]
            if r.get("detail"):
                entry["detail"] = r["detail"]
        else:
            entry["value"] = None
            entry["reason"] = r.get("reason") or "unavailable"
            entry["sample_value"] = sample_value(key)      # labelled sample, never real data
            warnings.append(f"{key}: {entry['reason']}")
        inputs[key] = entry

    result = score(values)
    return {"model": MODEL_VERSION, "generated_at": _now(), "fallback": False, "complete": result is not None,
            "inputs": inputs, "components": components(values), "agsi": result,
            "warnings": warnings, "disclaimer": DISCLAIMER}


def fallback_body(reason):
    """Same shape as a normal reply, every input unavailable with a labelled sample value."""
    return {"model": MODEL_VERSION, "generated_at": _now(), "fallback": True, "complete": False,
            "inputs": {k: {"value": None, "unit": spec[-1], "as_of": None, "source": None, "status": "unavailable",
                           "reason": reason, "sample_value": sample_value(k)} for k, spec in INPUTS.items()},
            "components": {}, "agsi": None, "warnings": [f"service fallback: {reason}"], "disclaimer": DISCLAIMER}


try:
    CACHE_SECONDS = max(0, int(os.getenv("AGSI_CACHE_SECONDS", "900") or 900))
except ValueError:
    CACHE_SECONDS = 900
_cache = {"at": 0.0, "body": None}
_lock = threading.Lock()


def latest_json():
    """Always returns a JSON string; never raises."""
    with _lock:
        if _cache["body"] and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["body"]
    try:
        body = json.dumps(build_latest(), separators=(",", ":"))
    except Exception as e:
        log.exception("build_latest failed")
        return json.dumps(fallback_body(f"the data service hit an unexpected error ({e.__class__.__name__})"),
                          separators=(",", ":"))   # not cached, so the next request retries
    with _lock:
        _cache.update(at=time.time(), body=body)
    return body


def cors_headers(origin=None):
    allowed = [o.strip() for o in os.getenv("AGSI_ALLOWED_ORIGINS", "*").split(",") if o.strip()]
    allow = "*" if (not allowed or "*" in allowed) else (origin if origin in allowed else allowed[0])
    h = {"Content-Type": "application/json; charset=utf-8",
         "Cache-Control": f"public, max-age=60, s-maxage={CACHE_SECONDS}, stale-while-revalidate=86400",
         "Access-Control-Allow-Origin": allow,
         "Access-Control-Allow-Methods": "GET, OPTIONS",
         "Access-Control-Allow-Headers": "Accept",
         "X-Content-Type-Options": "nosniff"}
    if allow != "*":
        h["Vary"] = "Origin"
    return h


# =============================================================== Vercel entry point
class handler(BaseHTTPRequestHandler):
    def _send(self, status, body=b""):
        try:
            hdrs = cors_headers(self.headers.get("Origin"))
        except Exception:
            hdrs = {"Content-Type": "application/json; charset=utf-8", "Access-Control-Allow-Origin": "*"}
        self.send_response(status)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        try:
            body = latest_json()
        except Exception as e:   # belt and braces: latest_json already never raises
            body = json.dumps(fallback_body(f"unexpected error ({e.__class__.__name__})"))
        self._send(200, body.encode("utf-8"))

    def log_message(self, fmt, *args):   # keep Vercel logs to warnings/errors
        pass
