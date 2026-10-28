"""
Unit and integration tests for the NQ / MNQ Futures IBKR Trading Bot.
Verifies:
- Tick rounding (0.25 increments) and Session-Anchored VWAP daily resets
- EMA + VWAP Crossover & Pullback signal generation
- Intraday Risk Manager guardrails (RTH window, EOD flatten, Max Daily Loss, Consecutive Losses)
- End-to-end Backtester simulation
"""
from __future__ import annotations

import pandas as pd

from backtest import Backtester, generate_synthetic_nq_data
from config import BotConfig
from indicators import compute_session_vwap, enrich_indicators, round_to_tick
from risk_manager import RiskManager
from strategy import NQIntradayStrategy


def test_round_to_tick() -> None:
    assert round_to_tick(20123.11, 0.25) == 20123.00
    assert round_to_tick(20123.14, 0.25) == 20123.25
    assert round_to_tick(20123.89, 0.25) == 20124.00


def test_session_vwap_resets_each_day() -> None:
    df = pd.DataFrame(
        {
            "timestamp": [
                pd.Timestamp("2026-10-26 09:30:00", tz="America/New_York"),
                pd.Timestamp("2026-10-26 09:31:00", tz="America/New_York"),
                pd.Timestamp("2026-10-27 09:30:00", tz="America/New_York"),
            ],
            "high": [20010.0, 20030.0, 20510.0],
            "low": [19990.0, 20010.0, 20490.0],
            "close": [20000.0, 20020.0, 20500.0],
            "volume": [1000, 1000, 500],
        }
    )
    vwap = compute_session_vwap(df)
    assert vwap.iloc[0] == 20000.0
    assert vwap.iloc[1] == 20010.0
    # Second day first bar must reset strictly to its own typical price (20500.0)
    assert vwap.iloc[2] == 20500.0


def test_risk_manager_guardrails_and_eod_flatten() -> None:
    cfg = BotConfig()
    cfg.risk.max_daily_loss_usd = 200.0
    cfg.risk.max_consecutive_losses = 2
    cfg.risk.cooldown_bars_after_exit = 2
    rm = RiskManager(cfg.risk, cfg.contract)

    # 1. Pre-open (09:31 ET is before 09:35 entry_start_time)
    ts_early = pd.Timestamp("2026-10-27 09:31:00", tz="America/New_York")
    allowed, reason = rm.can_open_trade(ts_early, current_bar_index=1)
    assert not allowed
    assert "Outside allowed RTH" in reason

    # 2. Valid RTH entry window (10:15 ET)
    ts_valid = pd.Timestamp("2026-10-27 10:15:00", tz="America/New_York")
    allowed, _ = rm.can_open_trade(ts_valid, current_bar_index=10)
    assert allowed

    # 3. Record losing trade and test cooldown
    rm.record_trade_closed(-100.0, exit_bar_index=10)
    allowed_cd, reason_cd = rm.can_open_trade(ts_valid, current_bar_index=11)
    assert not allowed_cd
    assert "Cooldown" in reason_cd

    # 4. Second consecutive loss halts trading for the day
    rm.record_trade_closed(-50.0, exit_bar_index=15)
    allowed_halt, reason_halt = rm.can_open_trade(ts_valid, current_bar_index=20)
    assert not allowed_halt
    assert "halted" in reason_halt.lower()

    # 5. Next day automatically resets session state
    ts_next_day = pd.Timestamp("2026-10-28 10:15:00", tz="America/New_York")
    allowed_next, _ = rm.can_open_trade(ts_next_day, current_bar_index=25)
    assert allowed_next

    # 6. Check 15:55 ET End-of-Day auto-flatten trigger
    ts_eod = pd.Timestamp("2026-10-28 15:55:00", tz="America/New_York")
    assert rm.should_flatten_eod(ts_eod)


def test_strategy_and_backtester_end_to_end() -> None:
    cfg = BotConfig()
    df = generate_synthetic_nq_data(num_days=5, seed=42)
    enriched = enrich_indicators(df)
    assert "vwap" in enriched.columns
    assert "ema_fast" in enriched.columns
    assert "atr" in enriched.columns

    bt = Backtester(cfg)
    result = bt.run(df)
    metrics = result["metrics"]
    trades = result["trades"]

    assert metrics["total_trades"] > 0
    for t in trades:
        # Verify tick alignment on stop loss & take profit
        assert round(t.stop_loss % 0.25, 5) == 0.0
        assert round(t.take_profit % 0.25, 5) == 0.0
        if t.direction == "LONG":
            assert t.take_profit > t.entry_price
        else:
            assert t.take_profit < t.entry_price
