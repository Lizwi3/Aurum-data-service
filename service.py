"""Builds the GET /api/agsi-latest response. Sources are fetched in parallel; results are cached
in memory for CACHE_SECONDS on a warm instance, and the response carries CDN cache headers."""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import core, sources

CACHE_SECONDS = int(os.getenv("AGSI_CACHE_SECONDS", "900"))
_cache = {"at": 0.0, "body": None}
_lock = threading.Lock()


def build_latest(fetchers=None):
    fetchers = fetchers or sources.FETCHERS
    with ThreadPoolExecutor(max_workers=len(fetchers)) as pool:
        futures = {k: pool.submit(fn) for k, fn in fetchers.items()}
        raw = {k: f.result() for k, f in futures.items()}

    inputs, values, warnings = {}, {}, []
    for key, (comp, vmin, vmax, *_rest, unit) in core.INPUTS.items():
        r = raw.get(key) or {"value": None, "status": "unavailable", "reason": "not fetched", "source": None, "as_of": None}
        v = r.get("value")
        if r.get("status") == "ok" and not core.valid(key, v):
            r = {**r, "value": None, "status": "unavailable",
                 "reason": f"value {v!r} is outside the accepted range {vmin:g} to {vmax:g}"}
        entry = {"value": r.get("value"), "unit": unit, "as_of": r.get("as_of"), "source": r.get("source"),
                 "status": r.get("status", "unavailable")}
        if entry["status"] != "ok":
            entry["reason"] = r.get("reason", "unavailable")
            warnings.append(f"{key}: {entry['reason']}")
        else:
            values[key] = entry["value"]
            if r.get("detail"):
                entry["detail"] = r["detail"]
        inputs[key] = entry

    comps = core.components(values)
    result = core.score(values)
    return {
        "model": core.MODEL_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "complete": result is not None,
        "inputs": inputs,
        "components": comps,          # 0-100 stress score per available input
        "agsi": result,               # null unless all five inputs are available
        "warnings": warnings,
        "disclaimer": "Research data for educational and analytical use only. Not financial, investment, "
                      "legal or trading advice.",
    }


def latest_json(force=False):
    with _lock:
        if not force and _cache["body"] and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["body"]
    body = json.dumps(build_latest(), separators=(",", ":"))
    with _lock:
        _cache.update(at=time.time(), body=body)
    return body


def headers(origin=None):
    allowed = [o.strip() for o in os.getenv("AGSI_ALLOWED_ORIGINS", "*").split(",") if o.strip()]
    allow = "*" if "*" in allowed else (origin if origin in allowed else allowed[0] if allowed else "")
    h = {"Content-Type": "application/json; charset=utf-8",
         "Cache-Control": f"public, max-age=60, s-maxage={CACHE_SECONDS}, stale-while-revalidate=86400",
         "Access-Control-Allow-Methods": "GET, OPTIONS",
         "Access-Control-Allow-Headers": "Accept",
         "X-Content-Type-Options": "nosniff"}
    if allow:
        h["Access-Control-Allow-Origin"] = allow
        if allow != "*":
            h["Vary"] = "Origin"
    return h
