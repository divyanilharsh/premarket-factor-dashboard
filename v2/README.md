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

Windows:

```bat
python v2\market_pulse_v2.py --port 8081
```

or double-click `Start Market Pulse V2.bat`.

macOS / Linux:

```bash
python3 v2/market_pulse_v2.py --port 8081
```

or use the launcher (double-click it in Finder, or run it from Terminal):

```bash
./v2/start_v2.command               # background + opens the browser
./v2/start_v2.command --no-open     # background, no browser
./v2/start_v2.command --foreground  # run in this terminal
./v2/start_v2.command --port 8082   # different port
./v2/start_v2.command --stop        # stop the background server
```

The launcher logs to `data/v2/logs/v2.log` and stores its pid in
`data/v2/v2.pid`.

The V1 dashboard can keep running on 8080 at the same time.

### Optional auto-start

Windows: double-click `v2\Install V2 AutoStart.bat`.  This creates a separate
Startup shortcut named `Pre-Market Pulse V2.lnk`.  The existing V1 Startup
shortcut and `Pre-Market Pulse AutoStart` task are not modified.

macOS: install a separate LaunchAgent that starts only V2 at login:

```bash
./v2/install_autostart_macos.sh          # default port 8081
./v2/install_autostart_macos.sh --port 8081
./v2/uninstall_autostart_macos.sh        # remove it again
```

The agent is `com.premarketpulse.v2` in `~/Library/LaunchAgents/` and logs to
`data/v2/logs/v2.launchd.log`.  V1 is not touched on either platform.

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

## Training data CSV

Build a supervised training file from the database:

```bat
python export_training_csv.py
```

Output:

```text
data\exports\training_data.csv
```

Each row is one valid hourly pair:

- **Inputs**: every factor's `delta` at the base mark, plus V2's four momentum
  inputs.
- **Target**: `target_next_pct` = next mark's NIFTY return in percent.
- Overnight/long-gap pairs are skipped, matching the V2 learner.
- `factor_count` tells how many factor deltas were available at that mark.
- The previous training CSV is replaced.

Double-click `Export Training Data CSV.bat` for the same result. The script
prints how many valid pairs exist and how many more are needed before the V2
neural net can train.

## Decision tree (VP / P / NE / N / VN)

A shallow decision tree predicts the **next hourly NIFTY return** as one of
five classes.  It is shared by both dashboards: one model, one set of class
cuts, trained from the V2 SQLite store.

| Class | Meaning |
|---|---|
| `VN` | very negative |
| `N`  | negative |
| `NE` | neutral |
| `P`  | positive |
| `VP` | very positive |

Bands are **quantiles** (20/40/60/80) of the training window, so classes stay
balanced as the sample grows.  The cut points are frozen into the model file
and reused at inference until the next retrain.

Inputs are point-in-time only: every factor's hourly `delta` (features with
under 40% coverage are dropped), the four momentum inputs, `nifty_day_pct`,
hour-of-day, factor count and bullish/bearish breadth.

```bash
python v2/train_tree.py                  # train + validate + export
python v2/train_tree.py --min-pairs 40   # preview only, not a score
python v2/selftest_tree.py               # labels, leakage, export, inference
```

Outputs:

| File | Contents |
|---|---|
| `data/v2/tree_model.json` | cuts, feature list, JSON tree, metrics |
| `data/exports/tree_rules.txt` | human-readable if/then rules |
| `data/exports/tree_confusion.csv` | walk-forward confusion matrix |
| `data/v2/tree_history.json` | cuts + scores of the last 200 retrains |

Only the tree is served.  RandomForest and HistGradientBoosting are scored on
the same expanding-window folds as benchmarks (1-mark embargo, no shuffling),
next to a majority-class baseline.

**Sample gate:** below `--min-pairs` (default **120**) the tree is still
exported but its status is `learning`.  The dashboards then show
`TREE learning (33/120)` and the API exposes an `in_sample` preview instead of
a `prediction`.  No accuracy is claimed below the gate.

Both dashboards expose the same payload:

```text
http://127.0.0.1:8080/api/tree    # V1
http://127.0.0.1:8081/api/tree    # V2
```

and show a `TREE` chip plus a `TREE - next-hour class` row in the hourly-track
category.  V1 hot-reloads `tree_model.json` when it changes; V2 retrains it in
the background after each new hourly mark (at most every 10 minutes).

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
