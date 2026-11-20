# V3 experiment log

## Isolation contract

V3 must never change these files:

```
v2/**                 (V2 code, launcher, docs)
tree_model.py         (shared inference)
market_mobile.py      (V1 fetchers, imported read-only)
install_autostart_macos.sh   (installs V1+V2)
```

Verification: hash every file above before and after a V3 change.

## What V3 changes vs V2

| Area | V2 | V3 |
|---|---|---|
| Data directory | `data/v2` | `data/v3` |
| Default port | 8081 | 8082 |
| Tree model path | `data/v2/tree_model.json` | `data/v3/tree_model.json` |
| Tree training script | `v2/train_tree.py` | `v3/train_tree.py` |
| Tree rules/confusion | `data/exports/tree_*` | `data/exports/v3_tree_*` |
| Retrain target | `v2/train_tree.py` | `v3/train_tree.py` |
| Login agent | `com.premarketpulse.v2` | `com.premarketpulse.v3` |
| Technical module | — | `v3/tech/*`, `/api/tech`, TECH chip + category |

The V2 and V3 Python files are otherwise identical, so a fix in one does not
automatically flow to the other. That is deliberate: V3 is a sandbox.

## Technical predictor (first experiment)

- EMA ribbon 20/50/100/150/200 on 1m, 5m, 1h, 1d, 1wk, 1mo for NIFTY 50.
- Independent score and class per timeframe (no cross-timeframe blending yet).
- Golden Gate checks: trend/ribbon order, compression, box breakout, volume
  surge, overextension invalidation.
- Scores and components are stored per bar in `data/v3/tech.db` (`signals`).
- 30-day EMA series available as CSV from `v3/tech/report.py`.

## Open items

- Intraday volume: waiting for a usable aggregate source. `^NSEI` intraday
  candles have zero volume; NSE's constituent-sum endpoint is not exposed.
  Until then intraday frames skip the volume rule. Daily+ uses real NSE
  archive volume.
- Daily/weekly/monthly use the NSE archive; intraday uses Yahoo, which is
  rate limited and may show `stale` until the cooldown clears.
- Monthly EMA200: low confidence by construction.
- Only the indicators listed above are implemented. RSI/MACD/ATR/etc. are not
  added until explicitly requested.
