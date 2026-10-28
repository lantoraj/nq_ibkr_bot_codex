# nq_ibkr_bot_codex

Local research fork of lantoraj/nq_ibkr_bot. See PROJECT_README.MD for historical baseline documentation; its performance claims do not describe this fork.

Run `python -m pytest -q` and `python main.py --mode backtest --csv data/mnq_2m_3months.csv`.
Run `python optimize.py` for a chronological parameter study using the same backtester.

Changes: corrected stop auxPrice, fill-based stop reference, account-scoped orders, asynchronous cancel/reconcile/flatten, SQLite execution deduplication and risk persistence, restart fail-closed checks, independent EOD watchdog, dynamic sizing budget enforcement, RSI and indicator configuration fixes, completed opening range, next-bar limit execution, gap stops, repeatable backtests, accurate trade-statistic naming.

Limitations: paper integration remains required. Stop amendments now require broker acknowledgement and rejection/timeout requests flattening. Automatic adoption of existing positions is deliberately unsupported. The backtest is OHLC-based with conservative stop-first ambiguity handling and cannot reconstruct intrabar fill order. Unfinished positions are flagged rather than included in realized metrics. Historical data was previously optimized; this is not an untouched out-of-sample result. The score is heuristic, not a calibrated probability. position_engine.py shares stop-management and execution-state rules between adapters. The historical simulator acknowledges immediately; the live adapter waits for broker events. Feed staleness blocks entries but does not imply broker protection is confirmed. Use a dedicated account/contract allocation; flatten covers that account's contract exposure.

## Execution verification

25 tests cover partial fills, duplicate executions and commission reports, delayed/stale acknowledgements, rejected amendments, missing protection timeouts, disconnects, reconciliation markers, and fills racing cancellation. Tests use a simulated broker and do not connect to IBKR. Restart with an unresolved persisted lifecycle remains blocked for operator reconciliation. Paper-account validation is still required for actual IBKR event ordering and child-order activation.
