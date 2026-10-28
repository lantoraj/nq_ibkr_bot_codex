"""
Intraday Risk & Session Guardrails for NQ / MNQ Futures Trading.
Enforces:
- RTH entry time window & mandatory End-of-Day (EOD) flatten check
- Daily max loss circuit breaker
- Max daily trades & max consecutive losses circuit breakers
- Post-exit bar cooldown
- Dynamic or fixed contract sizing
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from typing import Optional, Tuple

import pandas as pd
import pytz

from config import ContractConfig, RiskConfig


@dataclass
class DailyRiskState:
    """Tracks live or simulated intraday risk metrics for a single trading session."""
    session_date: Optional[date] = None
    realized_pnl_usd: float = 0.0
    trades_taken: int = 0
    consecutive_losses: int = 0
    last_exit_bar_index: int = -999
    halted: bool = False
    halt_reason: str = ""


class RiskManager:
    """Validates whether new signals are allowed and calculates contract size."""

    def __init__(self, risk_cfg: RiskConfig, contract_cfg: ContractConfig) -> None:
        self.r_cfg = risk_cfg
        self.c_cfg = contract_cfg
        self.tz = pytz.timezone(self.r_cfg.timezone)
        self.state = DailyRiskState()

        self._entry_start = self._parse_hhmm(self.r_cfg.entry_start_time)
        self._entry_end = self._parse_hhmm(self.r_cfg.entry_end_time)
        self._flatten_time = self._parse_hhmm(self.r_cfg.flatten_eod_time)

    @staticmethod
    def _parse_hhmm(hhmm: str) -> time:
        parts = hhmm.strip().split(":")
        return time(int(parts[0]), int(parts[1]))

    def _to_session_dt(self, ts: pd.Timestamp) -> pd.Timestamp:
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            return ts.tz_localize(self.tz)
        return ts.tz_convert(self.tz)

    def check_and_reset_session(self, ts: pd.Timestamp) -> None:
        """Reset daily counters automatically when a new trading date begins."""
        local_dt = self._to_session_dt(ts)
        curr_date = local_dt.date()
        if self.state.session_date != curr_date:
            self.state = DailyRiskState(session_date=curr_date)

    def should_flatten_eod(self, ts: pd.Timestamp) -> bool:
        """Return True if current time is at or past the mandatory EOD flatten time (e.g., 15:55 ET)."""
        if not self.r_cfg.rth_only:
            return False
        local_dt = self._to_session_dt(ts)
        return local_dt.time() >= self._flatten_time

    def is_within_entry_window(self, ts: pd.Timestamp) -> bool:
        """Return True if current timestamp falls within allowed intraday entry hours."""
        if not self.r_cfg.rth_only:
            return True
        local_dt = self._to_session_dt(ts)
        t = local_dt.time()
        return self._entry_start <= t <= self._entry_end

    def can_open_trade(
        self,
        ts: pd.Timestamp,
        current_bar_index: int = 0,
    ) -> Tuple[bool, str]:
        """
        Check all risk rules before submitting a new bracket order.
        Returns (allowed: bool, reason: str).
        """
        self.check_and_reset_session(ts)

        if self.state.halted:
            return False, f"Session halted: {self.state.halt_reason}"

        if not self.is_within_entry_window(ts):
            return False, "Outside allowed RTH entry time window"

        if self.state.realized_pnl_usd <= - abs(self.r_cfg.max_daily_loss_usd):
            self.state.halted = True
            self.state.halt_reason = (
                f"Max daily loss reached (${self.state.realized_pnl_usd:.2f} <= -${self.r_cfg.max_daily_loss_usd:.2f})"
            )
            return False, self.state.halt_reason

        if self.state.trades_taken >= self.r_cfg.max_daily_trades:
            return False, f"Max daily trades reached ({self.state.trades_taken}/{self.r_cfg.max_daily_trades})"

        if self.state.consecutive_losses >= self.r_cfg.max_consecutive_losses:
            self.state.halted = True
            self.state.halt_reason = (
                f"Max consecutive losses reached ({self.state.consecutive_losses})"
            )
            return False, self.state.halt_reason

        bars_since_exit = current_bar_index - self.state.last_exit_bar_index
        if bars_since_exit < self.r_cfg.cooldown_bars_after_exit:
            return False, f"Cooldown active ({bars_since_exit}/{self.r_cfg.cooldown_bars_after_exit} bars)"

        return True, "OK"

    def calculate_contracts(self, stop_distance_points: float) -> int:
        """
        Determine contract quantity based on fixed_contracts or dynamic dollar risk per trade.
        """
        if self.r_cfg.risk_per_trade_usd <= 0 or stop_distance_points <= 0:
            return min(self.r_cfg.fixed_contracts, self.r_cfg.max_contracts)

        dollar_risk_per_contract = stop_distance_points * self.c_cfg.point_value + self.r_cfg.estimated_round_trip_cost_usd
        remaining = max(0.0, self.r_cfg.max_daily_loss_usd + self.state.realized_pnl_usd)
        if dollar_risk_per_contract <= 0:
            return 1

        raw_qty = int(min(self.r_cfg.risk_per_trade_usd, remaining) // dollar_risk_per_contract)
        return max(0, min(raw_qty, self.r_cfg.max_contracts))

    def record_trade_closed(self, net_pnl_usd: float, exit_bar_index: int = 0) -> None:
        """Update session risk state after a position is closed."""
        self.state.trades_taken += 1
        self.state.realized_pnl_usd += net_pnl_usd
        self.state.last_exit_bar_index = exit_bar_index

        if net_pnl_usd < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

        if self.state.realized_pnl_usd <= -abs(self.r_cfg.max_daily_loss_usd):
            self.state.halted = True
            self.state.halt_reason = (
                f"Max daily loss breached (${self.state.realized_pnl_usd:.2f})"
            )
        elif self.state.consecutive_losses >= self.r_cfg.max_consecutive_losses:
            self.state.halted = True
            self.state.halt_reason = (
                f"Max consecutive losses breached ({self.state.consecutive_losses})"
            )
