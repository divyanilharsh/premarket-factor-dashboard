# Pre-Market Pulse V3 — Technical Lab

V3 is a **separate fork of V2** used for experiments. It does not modify V2 and
does not share its database, port, logs or launch agent.

| | V2 | V3 |
|---|---|---|
| Port | 8081 | **8082** |
| Code | `v2/market_pulse_v2.py` | `v3/market_pulse_v3.py` |
| Database | `data/v2/market.db` | `data/v3/market.db` (seeded once from V2) |
| Tree artifacts | `data/exports/tree_*` | `data/exports/v3_tree_*` |
| Login agent | `com.premarketpulse.v2` | `com.premarketpulse.v3` |

## Run

```bash
./v3/start_v3.command           # background + browser
./v3/start_v3.command --stop
./v3/start_v3.command --foreground
```

V3 also starts at login if you install its agent (V1/V2 agents are untouched):

```bash
./v3/install_autostart_macos.sh
./v3/uninstall_autostart_macos.sh
```

## Technical / Golden Gate module

`v3/tech/` computes an EMA ribbon (20 / 50 / 100 / 150 / 200) for **NIFTY 50**
on six timeframes — 1m, 5m, 1h, 1d, 1wk, 1mo — and evaluates the Golden Gate
rules independently on each one.

Output per timeframe:

- EMA values, distance from each EMA, ribbon spread % of close
- alignment (`bull` / `bear` / `mixed`)
- box high/low from the last 5–15 candles
- gate state: `none` / `compressing` / `forming` / `breakout` / `extended`
- score `-100..+100` and class `VN/N/NE/P/VP`

Rule weights live in `v3/tech/golden_gate.py` (`COMPRESSION_PCT`,
`TIGHT_RANGE_PCT`, `OVEREXTENDED_PCT`, `VOLUME_SURGE`, `CLASS_BANDS`).

### Sources and fetching

| Timeframes | Source | Why |
|---|---|---|
| 1d, 1wk, 1mo | NSE daily index archive (`ind_close_all_DDMMYYYY.csv`) | reliable, includes volume, no rate-limit war |
| 1m, 5m, 1h | Yahoo chart API | the only free intraday source, rate limited |

NSE daily files are cached one row per day in
`data/v3/candles/nse_daily.json`; weekly/monthly are resampled from them, so a
deeper history also deepens the higher timeframes. `^NSEI` intraday candles
carry no volume, so intraday frames report `volume_source: unavailable`.

Yahoo requests are spaced by **8 seconds** so V1/V2 keep their own quote
budget. Each timeframe is cached on disk with a TTL (1m 120s, 5m 5min, 1h
15min, 1d 1h, 1wk 6h, 1mo 24h). On HTTP 429 the cached series is served and
marked `stale`; the fetcher backs off five minutes instead of retrying into
the block.

One-time history backfill (both are resumable and safe to interrupt):

```bash
python3 v3/tech/nse_archive.py --days 1000   # daily/weekly/monthly
python3 v3/tech/backfill.py --gap 50         # 1m/5m/1h from Yahoo
```

The dashboards keep working with whatever is already cached.

History fetched for warmup:

| TF | Range | Typical bars |
|---|---|---|
| 1m | 28 days (7-day chunks) | ~9,000 |
| 5m | 60 days | ~4,500 |
| 1h | 730 days | ~5,000 |
| 1d | 1000 NSE trading days | 1000 |
| 1wk | resampled | ~200 |
| 1mo | resampled | ~49 |

### Endpoints

| Path | Contents |
|---|---|
| `/api/tech` | latest scan, all timeframes |
| `/api/tech` (payload) | per-timeframe EMAs, box, gate, score, class, 30-day points |
| `/api/data` | includes a `TECH 1d` chip and a `12. TECHNICAL / GOLDEN GATE` category |

CSV of the 30-day EMA series:

```bash
python3 v3/tech/report.py
# -> data/exports/v3_ema_30d.csv
```

### Tests

```bash
python3 v3/selftest.py           # V2 learner logic, unchanged
python3 v3/selftest_tree.py      # decision tree, V3 paths
```

## Known limitations

- **Monthly EMA200 is low confidence.** NIFTY has ~230 monthly bars; an EMA200
  needs far more to converge. The UI reports `conf low` rather than pretending.
- **Intraday volume is unavailable.** `^NSEI` 1-minute and 5-minute candles
  carry zero volume, and NSE does not expose aggregate index volume in a single
  public call right now. Intraday frames therefore skip the volume-surge check
  (`volume_source: unavailable`) and rely on close + EMA alignment + box break.
  Daily/weekly/monthly use real `^NSEI` volume.
- **Yahoo rate limits.** Backfill is deliberately slow. A burst of requests can
  trigger a temporary 429; cached data keeps the dashboard working.
- Positions/entries are **not** executed anywhere. This is a research view.
