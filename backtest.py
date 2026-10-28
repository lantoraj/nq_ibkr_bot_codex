"""
Event-driven Backtester & Synthetic NQ Intraday Data Generator.
Uses the exact same `NQIntradayStrategy` and `RiskManager` classes as the live IBKR runner.
Simulates:
- Bracket Order fills (Stop Loss, Take Profit, and 15:55 ET End-of-Day flatten)
- Commission & slippage modeling for NQ / MNQ futures
- Detailed performance report (Win Rate, Profit Factor, Max Drawdown, Sharpe, Expectancy)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config import BotConfig
from position_engine import PositionEngine
from indicators import round_to_tick
from risk_manager import RiskManager
from strategy import NQIntradayStrategy, TradeSignal


@dataclass
class SimulatedTrade:
    """Record of a completed backtest trade."""
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    direction: str
    reason: str
    contracts: int
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    exit_reason: str
    gross_points: float
    net_pnl_usd: float


def generate_synthetic_nq_data(
    num_days: int = 5,
    seed: int = 42,
    start_price: float = 20250.0,
) -> pd.DataFrame:
    """
    Generate realistic 1-minute RTH (09:30 - 16:00 ET) NQ futures bars with
    intraday trend regimes, pullbacks, and volume profiles for offline verification.
    """
    rng = np.random.default_rng(seed)
    rows = []
    base_dates = pd.bdate_range("2026-10-19", periods=num_days)
    price = start_price

    for day_idx, bdate in enumerate(base_dates):
        day_timestamps = pd.date_range(
            f"{bdate.strftime('%Y-%m-%d')} 09:30:00",
            f"{bdate.strftime('%Y-%m-%d')} 15:59:00",
            freq="1min",
            tz="America/New_York",
        )
        # Alternate bullish and bearish intraday trend regimes with periodic 5-8 bar pullbacks
        regime_dir = 1.0 if day_idx % 2 == 0 else -1.0

        for m_idx, ts in enumerate(day_timestamps):
            # Create structured trend + periodic pullback waves every ~25 minutes
            cycle_pos = m_idx % 28
            if cycle_pos < 20:
                drift = regime_dir * 0.85
            elif cycle_pos < 25:
                # Controlled pullback toward fast/slow EMA
                drift = -regime_dir * 1.15
            else:
                # Strong resumption bar reclaiming Fast EMA
                drift = regime_dir * 2.10

            noise = rng.normal(0, 2.2)
            delta = drift + noise

            open_p = round_to_tick(price)
            close_p = round_to_tick(open_p + delta)
            wick_up = abs(rng.normal(2.5, 1.2))
            wick_dn = abs(rng.normal(2.5, 1.2))

            high_p = round_to_tick(max(open_p, close_p) + wick_up)
            low_p = round_to_tick(min(open_p, close_p) - wick_dn)

            # Higher volume at open, close, and trend resumption bars
            u_shape = 1.6 if (m_idx < 30 or m_idx > 350) else 1.0
            resumption_boost = 1.35 if cycle_pos >= 25 else 1.0
            volume = int(max(150, rng.normal(950, 220) * u_shape * resumption_boost))

            rows.append(
                {
                    "timestamp": ts,
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "volume": volume,
                }
            )
            price = close_p

    return pd.DataFrame(rows)


class Backtester:
    """Bar-by-bar backtester for the NQ/MNQ Intraday Strategy."""

    def __init__(
        self,
        config: BotConfig,
        commission_per_side_usd: float = 0.62,  # Typical IBKR MNQ commission ($2.10 for NQ)
        slippage_ticks: int = 1,
    ) -> None:
        self.cfg = config
        self.strategy = NQIntradayStrategy(config.strategy, config.risk, config.contract)
        self.risk = RiskManager(config.risk, config.contract)
        self.commission_per_side = commission_per_side_usd
        self.slippage_points = slippage_ticks * config.contract.tick_size

    def run(self, df: pd.DataFrame) -> Dict[str, object]:
        """Run bar-by-bar simulation and return trade log and summary metrics."""
        self.risk = RiskManager(self.cfg.risk, self.cfg.contract)
        work_df = df.copy()
        work_df["timestamp"] = pd.to_datetime(work_df["timestamp"], utc=True).dt.tz_convert(self.cfg.risk.timezone)

        # Automatically resample 1-minute CSV data if strategy is configured for 2 mins or 5 mins
        if len(work_df) > 2:
            median_diff_sec = work_df["timestamp"].diff().dt.total_seconds().median()
            target_map = {"1 min": ("1min", 60), "2 mins": ("2min", 120), "5 mins": ("5min", 300)}
            rule, target_sec = target_map.get(self.cfg.strategy.bar_size, ("1min", 60))
            if median_diff_sec == 60 and target_sec > 60:
                work_df = (
                    work_df.set_index("timestamp")
                    .resample(rule, closed="left", label="left")
                    .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                    .dropna()
                    .reset_index()
                )

        enriched = self.strategy.prepare_dataframe(work_df)
        trades: List[SimulatedTrade] = []

        engine = None
        pending = None
        active_signal: Optional[TradeSignal] = None
        active_contracts: int = 0
        active_entry_price: float = 0.0
        active_stop_loss: float = 0.0
        active_entry_idx: int = 0
        be_moved: bool = False
        decay_moved: bool = False

        point_val = self.cfg.contract.point_value

        for i in range(len(enriched)):
            bar = enriched.iloc[i]
            ts = pd.Timestamp(bar["timestamp"])
            high_p = float(bar["high"])
            low_p = float(bar["low"])
            close_p = float(bar["close"])

            self.risk.check_and_reset_session(ts)

            # Entries become eligible only after the signal bar has closed.
            if pending is not None and active_signal is None:
                sig, submitted, qty, limit = pending
                expired = (ts.date() != sig.timestamp.date() or self.risk.should_flatten_eod(ts)
                           or (self.cfg.risk.cancel_unfilled_entry_bars > 0 and engine.entry_expired(i, self.cfg.risk.cancel_unfilled_entry_bars)))
                touched = low_p <= limit if sig.direction == "LONG" else high_p >= limit
                if expired:
                    engine.cancel_entry()
                    pending = None
                elif touched:
                    active_signal, active_contracts = sig, qty
                    op = float(bar["open"])
                    active_entry_price = min(op, limit) if sig.direction == "LONG" else max(op, limit)
                    active_stop_loss, active_entry_idx = sig.stop_loss, i
                    engine.fill(f"entry-{i}", qty, active_entry_price, i)
                    engine.acknowledge_stop(sig.stop_loss, qty)
                    be_moved = decay_moved = False
                    pending = None
                else:
                    continue
            # 1. Manage open position if one is active
            if active_signal is not None:
                exit_price: Optional[float] = None
                exit_reason = ""
                one_r = active_signal.stop_distance_points
                bars_held = i - active_entry_idx

                if active_signal.direction == "LONG":
                    if low_p <= active_stop_loss:
                        exit_price = min(active_stop_loss, float(bar["open"])) - self.slippage_points
                        exit_reason = (
                            "Break-Even Stop"
                            if be_moved
                            else ("Time-Decay Stop (-0.5R)" if decay_moved else "Stop Loss")
                        )
                    elif high_p >= active_signal.take_profit:
                        exit_price = active_signal.take_profit
                        exit_reason = "Take Profit"
                    elif self.risk.should_flatten_eod(ts):
                        exit_price = close_p - self.slippage_points
                        exit_reason = "EOD Flatten"
                else:  # SHORT
                    if high_p >= active_stop_loss:
                        exit_price = max(active_stop_loss, float(bar["open"])) + self.slippage_points
                        exit_reason = (
                            "Break-Even Stop"
                            if be_moved
                            else ("Time-Decay Stop (-0.5R)" if decay_moved else "Stop Loss")
                        )
                    elif low_p <= active_signal.take_profit:
                        exit_price = active_signal.take_profit
                        exit_reason = "Take Profit"
                    elif self.risk.should_flatten_eod(ts):
                        exit_price = close_p + self.slippage_points
                        exit_reason = "EOD Flatten"
                if exit_price is None:
                    proposed = engine.propose_stop(high_p, low_p, i, self.cfg.risk)
                    if proposed is not None:
                        engine.acknowledge_stop(proposed, active_contracts)
                        active_stop_loss = engine.stop
                        be_moved = engine.stop_reason == "Break-Even Stop"
                        decay_moved = engine.stop_reason == "Time-Decay Stop (-0.5R)"

                if exit_price is not None:
                    gross_pts = (
                        (exit_price - active_entry_price)
                        if active_signal.direction == "LONG"
                        else (active_entry_price - exit_price)
                    )
                    round_turn_comm = 2.0 * self.commission_per_side * active_contracts
                    net_pnl = (gross_pts * point_val * active_contracts) - round_turn_comm

                    trades.append(
                        SimulatedTrade(
                            entry_time=active_signal.timestamp,
                            exit_time=ts,
                            direction=active_signal.direction,
                            reason=active_signal.reason,
                            contracts=active_contracts,
                            entry_price=round(active_entry_price, 2),
                            exit_price=round(exit_price, 2),
                            stop_loss=active_stop_loss,
                            take_profit=active_signal.take_profit,
                            exit_reason=exit_reason,
                            gross_points=round(gross_pts, 2),
                            net_pnl_usd=round(net_pnl, 2),
                        )
                    )
                    engine.fill(f"exit-{i}", active_contracts, exit_price, i, entry=False)
                    self.risk.record_trade_closed(net_pnl, exit_bar_index=i)
                    active_signal = None
                    active_contracts = 0
                continue

            # 2. Check if new trade is allowed when flat
            allowed, _ = self.risk.can_open_trade(ts, current_bar_index=i)
            if not allowed:
                continue

            signal = self.strategy.evaluate_bar(enriched, idx=i)
            if signal is not None:
                qty = self.risk.calculate_contracts(signal.stop_distance_points)
                if qty > 0:
                    slip = self.cfg.risk.entry_limit_slippage_ticks * self.cfg.contract.tick_size
                    limit = signal.entry_price + (slip if signal.direction == "LONG" else -slip)
                    engine = PositionEngine(signal.direction, qty, signal.stop_loss, signal.stop_distance_points, i, self.cfg.contract.tick_size)
                    pending = (signal, i, qty, limit)

        metrics = self._compute_metrics(trades)
        return {
            "open_position_at_end": active_signal is not None,
            "pending_order_at_end": pending is not None,
            "trades": trades,
            "metrics": metrics,
            "trades_df": pd.DataFrame([t.__dict__ for t in trades]) if trades else pd.DataFrame(),
        }

    @staticmethod
    def _compute_metrics(trades: List[SimulatedTrade]) -> Dict[str, float]:
        if not trades:
            return {
                "total_trades": 0,
                "win_rate_pct": 0.0,
                "net_pnl_usd": 0.0,
                "profit_factor": 0.0,
                "max_drawdown_usd": 0.0,
                "avg_trade_usd": 0.0,
                "trade_pnl_t_stat": 0.0,
            }

        pnls = np.array([t.net_pnl_usd for t in trades], dtype=float)
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]

        gross_profit = float(wins.sum()) if len(wins) > 0 else 0.0
        gross_loss = float(abs(losses.sum())) if len(losses) > 0 else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        equity_curve = np.cumsum(pnls)
        peaks = np.maximum.accumulate(np.insert(equity_curve, 0, 0.0))[1:]
        drawdowns = peaks - equity_curve
        max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

        std_pnl = float(np.std(pnls, ddof=1)) if len(pnls) > 1 else 0.0
        sharpe = (float(np.mean(pnls)) / std_pnl * np.sqrt(len(pnls))) if std_pnl > 0 else 0.0

        return {
            "total_trades": len(trades),
            "win_rate_pct": round(100.0 * len(wins) / len(trades), 2),
            "net_pnl_usd": round(float(pnls.sum()), 2),
            "profit_factor": round(profit_factor, 2),
            "max_drawdown_usd": round(max_dd, 2),
            "avg_trade_usd": round(float(np.mean(pnls)), 2),
            "trade_pnl_t_stat": round(sharpe, 2),
        }
