# Pre-Market Pulse V2 - Long-Term Lab

This is a separate dashboard. It does **not** modify `market_mobile.py`, its
`data/mobile` files, its port, its startup task, or its UI.

## What is new

- **Separate process and port**: default `http://127.0.0.1:8081`
- **Separate SQLite store**: `data/v2/market.db`
- **Long-term power**: lagged correlation between a factor's hourly delta and
  the NEXT hourly NIFTY return, over all stored history, with Fisher-z
  small-sample shrinkage and an explicit confidence value.
- **Long-term effect**: learned correlation sign + robust current move, damped
  by sample confidence and signal strength. Missing or zero deltas are neutral
  (50). No all-time-max exaggeration.
- **New NN V2**: a small pure-Python MLP trained on point-in-time factor
  features, retrained on every hourly mark. It needs 60 time-ordered pairs
  before predicting, uses walk-forward validation, and shows OOS MAE/hit rate.
- **New GUI bars**: `LONG-TERM P/E`, `NN V2`, and a `LONG-TERM DATA` progress
  bar. Every factor row shows `P ... c...%`, `E ...`, and `n ...`.
- **At least 2,000 latest marks retained**; all marks are kept by default.

## Run

```bat
python v2\market_pulse_v2.py --port 8081
```

or double-click:

```text
Start Market Pulse V2.bat
```

The V1 dashboard can keep running on 8080 at the same time.

### Optional auto-start

To start V2 at logon as well (without changing the existing V1 auto-start),
double-click:

```text
v2\Install V2 AutoStart.bat
```

This creates a separate Startup shortcut named
`Pre-Market Pulse V2.lnk`.  The existing V1 Startup shortcut and
`Pre-Market Pulse AutoStart` task are not modified.  The shortcut was already
created during this rollout.

## Data sharing

On its first run, V2 reads `data/mobile/hourly.json` **once** to seed its new
database. It never writes that file. After that, V2 records its own hourly
marks every market hour.

The V1 data fetchers are reused read-only. `data/v2/fii_history.json` is the
only FII cache V2 writes; V1's own FII file is not touched.

## Endpoints

| Path | Contents |
|---|---|
| `/` | V2 dashboard |
| `/api/data` | V2 snapshot |
| `/api/status` | refresh status |
| `/api/marks?limit=24` | recent V2 marks |
| `/marks.json` | full V2 mark log |
| `/api/model` | current V2 model state |
| `/export.csv` | CSV of the current snapshot |
| `/api/health` | health check |

## Full database CSV export

The standalone export app is at the project root:

```bat
python export_market_csv.py
```

It reads the complete V2 SQLite database (no row limit), writes every mark and
factor to one CSV, and replaces the previous export so only one CSV remains:

```text
data\exports\market_data.csv
```

The CSV is saved as UTF-8 with BOM so Excel shows rupee/currency symbols
correctly. Layout is one row per factor per mark, with the NIFTY fields repeated
on each row and predictions joined by timestamp.

Double-click `Export Market Data CSV.bat` for the same result. Optional paths:

```bat
python export_market_csv.py --db data\v2\backups\market-20261111.db
python export_market_csv.py --out D:\temp\market_data.csv
```

The exporter opens SQLite in read-only/query-only mode. If the previous CSV is
open in Excel, close it and run again so it can be replaced.

## Self-test

```bat
python v2\selftest.py
```

The self-test uses temporary files only. It checks positive/negative/noise,
zero and missing deltas, an outlier, the learned sign, the neural net,
walk-forward validation, and prediction scoring.

## Important

- The PC must be running during market hours to collect hourly marks.
- 2,000 hourly marks at ~6-7 marks/day takes many months. Accuracy after
  1-3 months can come from 130-450 pairs; 2,000 is the long-term buffer.
- A daily SQLite backup is written to `data/v2/backups/` and the last 30
  daily copies are kept.
- This is a local research dashboard, not investment advice.
