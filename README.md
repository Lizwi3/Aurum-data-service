# AURUM data service (Vercel)

This is a small website plus one API endpoint:

- `/`: the AURUM page (`index.html`), already connected to the endpoint.
- `GET /api/agsi-latest` (also `/api/agsi_latest`): fetches the latest public data, works out the five AGSI inputs, scores them with model **agsi-1.1.0**, and returns JSON.

The whole service is one file, `api/agsi_latest.py`. It uses only Python's built-in libraries, so Vercel has nothing else to find or install. The optional `xlrd` package, listed in `requirements.txt`, is only used to read CME's `.xls` report.

The API keys stay on the server. The page never sees them.

## Where each figure comes from

| AGSI input | Source | How it's worked out | Updates |
|---|---|---|---|
| Registered gold inventory | CME Group `Gold_Stocks.xls` (COMEX depository report) | "TOTAL REGISTERED" ÷ 1,000,000 → million troy oz | Daily |
| Open interest | CFTC Commitments of Traders, legacy futures-only (Socrata dataset `6dca-aqww`, gold code `088691`) | `open_interest_all` ÷ 1,000 → thousand contracts | Weekly (Tuesday data, published Friday) |
| ETF flow | SPDR Gold Shares (GLD) daily archive CSV | Tonnes held today minus tonnes 7 days earlier; negative = outflow | Daily |
| Delivery ratio | Not automated | CME only publishes delivery notices as PDFs, so the page fills this one with a labelled sample figure. Type in the real number to override it. | – |
| Macro stress | FRED: `STLFSI4` (St. Louis Fed Financial Stress Index) and `DFII10` (10-yr real yield) | 0.5 × clamp(50 + 25 × STLFSI4) + 0.5 × clamp(abs(20-day change in DFII10) ÷ 0.50 × 100) | Daily / weekly |

**Why not Yahoo Finance for ETF flow?** Trading volume counts shares changing hands. It can't show whether gold left or entered the fund, and AGSI needs that direction (outflow = stress). GLD's own published holdings in tonnes measure the real flow. The catch is that only GLD is covered, not every gold ETF.

The macro formula is a starting assumption, like the other model ranges. It is documented, but not validated.

## Deploy (no terminal needed)

1. **Get a free FRED key** at fred.stlouisfed.org → My Account → API Keys.
2. **Put this folder on GitHub:** create a new repository and use *Add file → Upload files*. Drag in everything inside this `serverless` folder, not the folder itself, then commit.
3. **Create the site on Vercel:** sign in at vercel.com with GitHub → *Add New… → Project* → pick the repository → *Deploy*.
4. **Add the key:** Project → *Settings → Environment Variables* → add `FRED_API_KEY`. Then go to *Deployments* → ⋯ → *Redeploy*.
5. Open `https://<your-project>.vercel.app`. Go to **AGSI Engine → Auto-fill latest data**, and the figures should arrive with green LIVE tags.

Check the raw data any time at `https://<your-project>.vercel.app/api/agsi-latest`.

## Updating an existing deployment (fixes "could not import agsi-latest.py")

Earlier versions used `api/agsi-latest.py` plus an `agsi_service/` folder. Vercel couldn't load that combination. To update:

1. In your GitHub repository, **delete** `api/agsi-latest.py` and the whole `agsi_service` folder. Open each file → ⋯ → *Delete file* → *Commit changes*.
2. *Add file → Upload files*: drag in everything from this zip and commit. It replaces `index.html`, `vercel.json` and `requirements.txt`, and adds `api/agsi_latest.py`.
3. Vercel redeploys on its own. Open `https://<your-project>.vercel.app/api/agsi_latest` and you should see JSON.

If an old `api/agsi-latest.py` is left behind, the `/api/agsi-latest` address keeps failing. The page uses `/api/agsi_latest`, so it still works either way.

## Settings (environment variables)

| Name | Needed? | What it does |
|---|---|---|
| `FRED_API_KEY` | Yes, for macro stress | FRED access |
| `CFTC_APP_TOKEN` | Optional | Raises the CFTC rate limit |
| `AGSI_ALLOWED_ORIGINS` | Optional | Sites allowed to call the API from a browser, comma-separated. The default is `*`. Set your own domain for production. |
| `AGSI_CACHE_SECONDS` | Optional | How long results are reused (default 900 = 15 minutes) |

## When something fails

The endpoint never answers with a server error:
- If one source fails, that input comes back with `"status": "unavailable"`, a `reason`, and a `sample_value`. The page shows that figure with a SAMPLE tag.
- If everything fails, the reply has the same shape with `"fallback": true`.

Real data only ever appears in `value`. Sample numbers only ever appear in `sample_value`. Failures are written to Vercel's runtime logs.

| Reason shown | What to do |
|---|---|
| `FRED_API_KEY is not set on the server` | Add the key in Vercel → Settings → Environment Variables, then redeploy |
| `the xlrd package is not installed` | Make sure `requirements.txt` is at the root of the repository |
| `the source answered with HTTP 403` | The publisher blocked the automated download (CME sometimes does). That figure is a sample until it clears. |

## Response shape

```json
{
  "model": "agsi-1.1.0",
  "generated_at": "2026-10-21T18:00:00Z",
  "complete": false,
  "inputs": {
    "open_interest": {"value": 512.345, "unit": "thousand contracts", "as_of": "2026-10-13",
                      "source": "CFTC Commitments of Traders ...", "status": "ok", "detail": {...}},
    "delivery_ratio": {"value": null, "status": "unavailable", "reason": "...", "sample_value": 6.4}
  },
  "components": {"paper": 53.09},
  "agsi": null,
  "fallback": false,
  "warnings": ["delivery_ratio: ..."]
}
```

`components` holds the 0–100 stress score for each input that is available. `agsi` (score, heat, regime) is filled only when all five inputs are available.

## Run it on your own computer

```bash
pip install -r requirements.txt
export FRED_API_KEY=...        # Windows: set FRED_API_KEY=...
python dev_server.py           # http://127.0.0.1:8787
```

## Keeping things in sync

- After changing `../standalone/aurum.html`, run `python sync_page.py` to rebuild `index.html`.
- The main app's tests fail if this backend's maths, the page and the app ever disagree, or if `index.html` is out of date.

## Before going public

- **Data terms:** CME and SPDR data are free to view, but their terms limit redistribution. Read them before showing their figures to paying users.
- **Untested against the live sources:** the source parsers were written to each publisher's file format. They have been tested against sample files, not live downloads. If a publisher changes its format, that one figure shows "unavailable" with a reason, and the rest keep working.
- **CME blocking:** CME sometimes blocks automated downloads. If the registered inventory figure keeps showing as unavailable, that is the likely cause.
