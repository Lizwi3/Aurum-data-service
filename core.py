"""AGSI model agsi-1.1.0: the same maths as the AURUM app (aurum/engine.py) and the web page
(aurum.html). Kept dependency-free so it runs in a serverless function.
tests/test_serverless.py in the main app fails if this file drifts from aurum/engine.py."""
import math

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

# lower edge (inclusive), heat, regime
BANDS = [(0.0, "Calm", "Calm accumulation phase"),
         (30.0, "Watch", "Transition phase"),
         (60.0, "Rising Risk", "Liquidity stress phase"),
         (80.0, "System Stress", "Crisis regime")]


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
    return isinstance(value, (int, float)) and math.isfinite(value) and lo <= value <= hi


def components(values):
    """0-100 stress score for every input that has a valid value."""
    out = {}
    for key, (comp, _, _, low, high, up, _) in INPUTS.items():
        v = values.get(key)
        if v is not None and valid(key, v):
            out[comp] = normalize(v, low, high, up)
    return out


def score(values):
    """Full AGSI result, or None unless all five inputs are valid."""
    comp = components(values)
    if len(comp) != len(INPUTS):
        return None
    contrib = {c: round_half_up(comp[c] * WEIGHTS[c], 4) for c in WEIGHTS}
    total = 0.0
    for c in WEIGHTS:                      # fixed order, same as the app and page
        total += contrib[c]
    s = round_half_up(min(100.0, max(0.0, total)), 2)
    return {"score": s, "contributions": contrib, **classify(s)}
