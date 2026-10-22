"""Fetch the five AGSI inputs from public data sources.

Each fetcher returns a dict:
    {"value": float|None, "as_of": "YYYY-MM-DD"|None, "source": str, "status": "ok"|"unavailable",
     "reason": str (when unavailable), "detail": {...}}
Fetchers never raise; a failure becomes status "unavailable" with a reason, and the page then
fills that one figure with labelled sample data.

Parsing is split from downloading (parse_* functions) so it can be tested without the network.
"""
import csv
import io
import logging
import os
import re
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger("agsi.sources")
TIMEOUT = float(os.getenv("AGSI_HTTP_TIMEOUT", "8"))
UA = {"User-Agent": "AURUM-Intelligence/1.1 (+research; contact via site owner)"}

CFTC_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"   # Legacy COT, futures only
CFTC_GOLD_CODE = "088691"                                              # Gold, COMEX
GLD_URL = os.getenv("AGSI_GLD_CSV_URL", "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv")
CME_STOCKS_URL = os.getenv("AGSI_CME_STOCKS_URL", "https://www.cmegroup.com/delivery_reports/Gold_Stocks.xls")
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


def _ok(value, as_of, source, **detail):
    return {"value": value, "as_of": as_of, "source": source, "status": "ok", "detail": detail}


def _na(source, reason):
    return {"value": None, "as_of": None, "source": source, "status": "unavailable", "reason": reason}


def _get(url, **kw):
    r = requests.get(url, timeout=TIMEOUT, headers={**UA, **kw.pop("headers", {})}, **kw)
    r.raise_for_status()
    return r


def _why(e):
    if isinstance(e, requests.Timeout):
        return "the source took too long to answer"
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"the source answered with HTTP {e.response.status_code}"
    if isinstance(e, requests.RequestException):
        return "the source could not be reached"
    return str(e)[:160] or e.__class__.__name__


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


# ------------------------------------------------------------------ open interest (CFTC)
SRC_CFTC = "CFTC Commitments of Traders (legacy, futures only): Gold, COMEX"


def parse_cftc(rows):
    if not rows:
        raise ValueError("no gold rows in the CFTC response")
    row = rows[0]
    oi = float(row["open_interest_all"])
    return _ok(round(oi / 1000.0, 3), str(row["report_date_as_yyyy_mm_dd"])[:10], SRC_CFTC,
               contracts=int(oi), market=row.get("market_and_exchange_names"))


def open_interest():
    try:
        headers = {"X-App-Token": os.environ["CFTC_APP_TOKEN"]} if os.getenv("CFTC_APP_TOKEN") else {}
        r = _get(CFTC_URL, headers=headers, params={
            "cftc_contract_market_code": CFTC_GOLD_CODE,
            "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": 1})
        return parse_cftc(r.json())
    except Exception as e:
        log.warning("CFTC failed: %s", e)
        return _na(SRC_CFTC, _why(e))


# ------------------------------------------------------------------ ETF flow (GLD holdings)
SRC_GLD = "SPDR Gold Shares (GLD) published holdings, 7-day change"


def _parse_date(text):
    text = (text or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _num(text):
    try:
        return float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_gld_csv(text, days=7):
    """Net change in GLD gold holdings (tonnes) over `days` calendar days, from the trust's
    daily archive CSV. Finds the date column and the tonnes column by header name."""
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
        return parse_gld_csv(_get(GLD_URL).text)
    except Exception as e:
        log.warning("GLD failed: %s", e)
        return _na(SRC_GLD, _why(e))


# ------------------------------------------------------------------ registered inventory (CME)
SRC_CME = "CME Group COMEX gold depository report (Gold_Stocks.xls)"
_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def parse_cme_rows(rows):
    """rows: list of lists of cell values from the COMEX gold stocks sheet.
    Uses the last number on the 'TOTAL REGISTERED' row (today's total, troy oz)."""
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
        import xlrd
        book = xlrd.open_workbook(file_contents=_get(CME_STOCKS_URL).content)
        sh = book.sheet_by_index(0)
        return parse_cme_rows([sh.row_values(i) for i in range(sh.nrows)])
    except Exception as e:
        log.warning("CME stocks failed: %s", e)
        return _na(SRC_CME, _why(e))


# ------------------------------------------------------------------ delivery ratio
def delivery_ratio():
    return _na("CME Group delivery notices",
               "CME publishes delivery notices only as PDF reports, which this version does not read automatically")


# ------------------------------------------------------------------ macro stress (FRED)
SRC_FRED = "FRED: STLFSI4 financial stress index and DFII10 10-year real yield"
MACRO_METHOD = ("macro_stress = 0.5 x clamp(50 + 25 x STLFSI4, 0, 100) "
                "+ 0.5 x clamp(|20-day change in DFII10| / 0.50 x 100, 0, 100)")


def _fred(series_id, key, limit):
    r = _get(FRED_URL, params={"series_id": series_id, "api_key": key, "file_type": "json",
                               "sort_order": "desc", "limit": limit})
    obs = [(o["date"], float(o["value"])) for o in r.json().get("observations", []) if o.get("value") not in (".", "", None)]
    if not obs:
        raise ValueError(f"FRED returned no data for {series_id}")
    return obs   # newest first


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
    key = os.getenv("FRED_API_KEY", "")
    if not key:
        return _na(SRC_FRED, "FRED_API_KEY is not set on the server")
    try:
        return parse_macro(_fred("STLFSI4", key, 4), _fred("DFII10", key, 40))
    except Exception as e:
        log.warning("FRED failed: %s", e)
        return _na(SRC_FRED, _why(e))


FETCHERS = {
    "registered_inventory": registered_inventory,
    "open_interest": open_interest,
    "etf_flow": etf_flow,
    "delivery_ratio": delivery_ratio,
    "macro_stress": macro_stress,
}
