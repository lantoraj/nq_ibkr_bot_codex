"""
Core Strategy Engine for NQ / MNQ Intraday Trading (v4 Quant Engine).
Evaluates enriched OHLCV + Quantitative State-Space Indicators and produces structured
TradeSignal objects with calibrated Meta-Labeling confidence scores.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
import pandas as pd

from config import ContractConfig, RiskConfig, StrategyConfig
from indicators import enrich_indicators, round_to_tick


SignalDirection = Literal["LONG", "SHORT"]


@dataclass
class TradeSignal:
    """Actionable signal produced by the v4 Quant Strategy Engine."""
    timestamp: pd.Timestamp
    direction: SignalDirection
    reason: str
    entry_price: float
    stop_loss: float
    take_profit: float
    stop_distance_points: float
    atr: float
    rsi: float
    vwap: float
    ema_fast: float
    ema_slow: float
    meta_probability: float = 1.0
    kalman_vel_atr: float = 0.0
    hurst: float = 0.5


class NQIntradayStrategy:
    """
    v4 Quant Engine: Hybrid EMA(9/21/50) + Session VWAP Z-Band + PDH/PDL + ORB Strategy
    gated by a Two-Stage Lopez de Prado Meta-Labeling Confidence Classifier using:
    - 1D Constant-Velocity Kalman Filter State Velocity (`kalman_vel_atr`)
    - Variance-Ratio Implied Hurst Exponent (`hurst`)
    - VWAP Z-Score Optimality (`vwap_z`) & Candle Body Conviction (`body_ratio`)
    """

    def __init__(
        self,
        strategy_cfg: StrategyConfig,
        risk_cfg: RiskConfig,
        contract_cfg: ContractConfig,
    ) -> None:
        self.s_cfg = strategy_cfg
        self.r_cfg = risk_cfg
        self.c_cfg = contract_cfg

    def prepare_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute all required technical and quantitative indicators on an OHLCV dataframe."""
        return enrich_indicators(
            df,
            fast_ema=self.s_cfg.fast_ema_period,
            slow_ema=self.s_cfg.slow_ema_period,
            trend_ema=self.s_cfg.trend_ema_period,
            atr_period=self.s_cfg.atr_period,
            rsi_period=self.s_cfg.rsi_period,
            timezone=self.r_cfg.timezone,
            kalman_q_pos=self.s_cfg.kalman_q_pos, kalman_q_vel=self.s_cfg.kalman_q_vel,
            kalman_r_meas=self.s_cfg.kalman_r_meas, hurst_lag_q=self.s_cfg.hurst_lag_q,
            hurst_window=self.s_cfg.hurst_window,
        )

    @staticmethod
    def compute_meta_probability(
        direction: SignalDirection,
        kalman_vel_atr: float,
        hurst: float,
        body_ratio: float,
        vwap_z: float,
    ) -> float:
        """
        Second-Stage Logistic Meta-Labeling Classifier:
        Computes an uncalibrated heuristic score = 1 / (1 + exp(-z_logit))
        from normalized Kalman velocity, Hurst exponent, candle body conviction, and VWAP Z-score.
        """
        signed_k = kalman_vel_atr if direction == "LONG" else -kalman_vel_atr
        signed_k_clipped = float(np.clip(signed_k, -0.5, 0.8))
        vz_opt = 1.0 - abs(abs(vwap_z) - 1.1) / 1.2

        z_logit = (
            0.35
            + 1.8 * signed_k_clipped
            + 1.5 * (hurst - 0.42)
            + 0.6 * (body_ratio - 0.50)
            + 0.4 * vz_opt
        )
        z_clipped = float(np.clip(z_logit, -15.0, 15.0))
        return float(1.0 / (1.0 + np.exp(-z_clipped)))

    def evaluate_bar(self, enriched_df: pd.DataFrame, idx: int = -1) -> Optional[TradeSignal]:
        """
        Evaluate a specific bar index (defaults to latest bar `-1`) on an already-enriched DataFrame.
        Returns a TradeSignal if all primary & meta-labeling criteria are satisfied, or None otherwise.
        """
        n = len(enriched_df)
        target_idx = n + idx if idx < 0 else idx

        warmup = max(self.s_cfg.trend_ema_period, self.s_cfg.slow_ema_period, self.s_cfg.atr_period) + 5
        if target_idx < warmup or target_idx >= n:
            return None

        curr = enriched_df.iloc[target_idx]
        prev = enriched_df.iloc[target_idx - 1]

        atr = float(curr["atr"])
        rsi = float(curr["rsi"])
        vol_ratio = float(curr["vol_ratio"])
        spread_atr = float(curr["ema_spread_atr"])
        slope_atr = float(curr["slow_slope_atr"])
        body_ratio = float(curr["body_ratio"])
        upper_wick = float(curr["upper_wick_ratio"])
        lower_wick = float(curr["lower_wick_ratio"])
        vwap_z = float(curr["vwap_z"])
        kalman_vel_atr = float(curr["kalman_vel_atr"])
        hurst = float(curr["hurst"])

        # 1. Volatility, Trend Spread, Candle Body, Volume & VWAP Z-Score Band filters
        if pd.isna(atr) or atr < self.s_cfg.min_atr_points or atr > self.s_cfg.max_atr_points:
            return None
        if spread_atr < self.s_cfg.min_ema_spread_atr:
            return None
        if body_ratio < self.s_cfg.min_body_ratio:
            return None
        if vol_ratio < self.s_cfg.min_volume_ratio:
            return None
        if self.s_cfg.use_vwap_z_filter:
            abs_z = abs(vwap_z)
            if abs_z < self.s_cfg.min_vwap_z_score or abs_z > self.s_cfg.max_vwap_z_score:
                return None

        close_p = float(curr["close"])
        open_p = float(curr["open"])
        high_p = float(curr["high"])
        low_p = float(curr["low"])
        prev_high = float(prev["high"])
        prev_low = float(prev["low"])
        prev_3_high = float(curr["prev_3_high"])
        prev_3_low = float(curr["prev_3_low"])
        orb_high = float(curr["orb_high"]) if pd.notna(curr["orb_high"]) else float("nan")
        orb_low = float(curr["orb_low"]) if pd.notna(curr["orb_low"]) else float("nan")
        pdh = float(curr["pdh"]) if pd.notna(curr["pdh"]) else float("nan")
        pdl = float(curr["pdl"]) if pd.notna(curr["pdl"]) else float("nan")

        ema_f = float(curr["ema_fast"])
        ema_s = float(curr["ema_slow"])
        ema_t = float(curr["ema_trend"])
        prev_ema_f = float(prev["ema_fast"])
        prev_ema_s = float(prev["ema_slow"])
        vwap = float(curr["vwap"])

        # 2. VWAP + 50 EMA + Slow EMA Slope Regime Filters
        bull_vwap = (close_p > vwap) if self.s_cfg.use_session_vwap else True
        bear_vwap = (close_p < vwap) if self.s_cfg.use_session_vwap else True

        bull_trend_ema = (close_p > ema_t) if self.s_cfg.require_trend_ema_alignment else True
        bear_trend_ema = (close_p < ema_t) if self.s_cfg.require_trend_ema_alignment else True

        bull_regime = (
            bull_vwap
            and bull_trend_ema
            and (ema_f > ema_s)
            and (slope_atr > self.s_cfg.min_slow_slope_atr)
        )
        bear_regime = (
            bear_vwap
            and bear_trend_ema
            and (ema_f < ema_s)
            and (slope_atr < -self.s_cfg.min_slow_slope_atr)
        )

        # 3. Prior Day High / Low (PDH / PDL) Overhead Resistance & Support Guard
        if self.s_cfg.use_pdh_pdl_guard:
            buf = self.s_cfg.pdh_pdl_buffer_atr * atr
            if pd.notna(pdh) and close_p < pdh and (pdh - close_p) < buf:
                bull_regime = False
            if pd.notna(pdl) and close_p > pdl and (close_p - pdl) < buf:
                bear_regime = False

        bull_candle = (close_p > open_p) and (upper_wick <= self.s_cfg.max_opposing_wick_ratio)
        bear_candle = (close_p < open_p) and (lower_wick <= self.s_cfg.max_opposing_wick_ratio)
        tol = self.s_cfg.pullback_ema_tolerance_atr * atr
        mode = self.s_cfg.entry_mode

        signal_dir: Optional[SignalDirection] = None
        reason = ""

        # --- Check LONG setups ---
        if bull_regime and bull_candle:
            # A) Bullish EMA Pullback Rejection
            if mode in ("pullback", "both", "hybrid_all") and (45.0 <= rsi <= 68.0):
                touched_ema_zone = (low_p <= ema_f + tol) and (low_p >= ema_s - tol)
                reclaimed_fast_ema = close_p > ema_f
                broke_prev_high = (close_p > prev_high) if self.s_cfg.require_prev_extreme_break else True
                if touched_ema_zone and reclaimed_fast_ema and broke_prev_high:
                    signal_dir = "LONG"
                    reason = f"Bullish Pullback off EMA({self.s_cfg.fast_ema_period}) > VWAP & EMA50"

            # B) Bullish EMA Crossover
            if signal_dir is None and mode in ("crossover", "both", "hybrid_all") and (50.0 <= rsi <= 70.0):
                if prev_ema_f <= prev_ema_s and ema_f > ema_s and close_p > ema_f:
                    signal_dir = "LONG"
                    reason = f"Bullish EMA({self.s_cfg.fast_ema_period}/{self.s_cfg.slow_ema_period}) Crossover > VWAP"

            # C) 3-Bar Momentum Continuation Breakout
            if signal_dir is None and mode == "hybrid_all" and (52.0 <= rsi <= 70.0) and vol_ratio >= 1.1:
                if close_p > prev_3_high and (close_p - ema_f) <= 1.2 * atr:
                    signal_dir = "LONG"
                    reason = "Bullish 3-Bar Momentum Breakout > VWAP & EMA50"

            # D) Opening Range High Breakout
            if signal_dir is None and mode == "hybrid_all" and pd.notna(orb_high) and (52.0 <= rsi <= 72.0):
                if prev_high <= orb_high and close_p > orb_high:
                    signal_dir = "LONG"
                    reason = "Bullish Opening Range Breakout (ORB High) > VWAP"

        # --- Check SHORT setups ---
        if signal_dir is None and bear_regime and bear_candle:
            # A) Bearish EMA Pullback Rejection
            if mode in ("pullback", "both", "hybrid_all") and (32.0 <= rsi <= 55.0):
                touched_ema_zone = (high_p >= ema_f - tol) and (high_p <= ema_s + tol)
                rejected_fast_ema = close_p < ema_f
                broke_prev_low = (close_p < prev_low) if self.s_cfg.require_prev_extreme_break else True
                if touched_ema_zone and rejected_fast_ema and broke_prev_low:
                    signal_dir = "SHORT"
                    reason = f"Bearish Pullback off EMA({self.s_cfg.fast_ema_period}) < VWAP & EMA50"

            # B) Bearish EMA Crossover
            if signal_dir is None and mode in ("crossover", "both", "hybrid_all") and (30.0 <= rsi <= 50.0):
                if prev_ema_f >= prev_ema_s and ema_f < ema_s and close_p < ema_f:
                    signal_dir = "SHORT"
                    reason = f"Bearish EMA({self.s_cfg.fast_ema_period}/{self.s_cfg.slow_ema_period}) Crossover < VWAP"

            # C) 3-Bar Momentum Continuation Breakdown
            if signal_dir is None and mode == "hybrid_all" and (30.0 <= rsi <= 48.0) and vol_ratio >= 1.1:
                if close_p < prev_3_low and (ema_f - close_p) <= 1.2 * atr:
                    signal_dir = "SHORT"
                    reason = "Bearish 3-Bar Momentum Breakdown < VWAP & EMA50"

            # D) Opening Range Low Breakdown
            if signal_dir is None and mode == "hybrid_all" and pd.notna(orb_low) and (28.0 <= rsi <= 48.0):
                if prev_low >= orb_low and close_p < orb_low:
                    signal_dir = "SHORT"
                    reason = "Bearish Opening Range Breakdown (ORB Low) < VWAP"

        if signal_dir is None:
            return None

        # 4. v4 Quant Meta-Labeling Confidence Gate
        meta_prob = self.compute_meta_probability(
            direction=signal_dir,
            kalman_vel_atr=kalman_vel_atr,
            hurst=hurst,
            body_ratio=body_ratio,
            vwap_z=vwap_z,
        )
        if self.s_cfg.use_quant_meta_filter and meta_prob < self.s_cfg.min_meta_probability:
            return None

        # 5. Calculate Stop Loss & Take Profit levels rounded to NQ 0.25 tick increments
        raw_stop_dist = (
            atr * self.r_cfg.atr_stop_multiplier
            if self.r_cfg.stop_mode == "atr"
            else self.r_cfg.fixed_stop_points
        )
        stop_dist = max(self.r_cfg.min_stop_points, min(self.r_cfg.max_stop_points, round_to_tick(raw_stop_dist)))
        tp_dist = round_to_tick(stop_dist * self.r_cfg.reward_risk_ratio)

        tick = self.c_cfg.tick_size
        entry_price = round_to_tick(close_p, tick)

        if signal_dir == "LONG":
            stop_loss = round_to_tick(entry_price - stop_dist, tick)
            take_profit = round_to_tick(entry_price + tp_dist, tick)
        else:
            stop_loss = round_to_tick(entry_price + stop_dist, tick)
            take_profit = round_to_tick(entry_price - tp_dist, tick)

        actual_stop_dist = round(abs(entry_price - stop_loss), 2)

        return TradeSignal(
            timestamp=pd.Timestamp(curr["timestamp"]),
            direction=signal_dir,
            reason=reason,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            stop_distance_points=actual_stop_dist,
            atr=round(atr, 2),
            rsi=round(rsi, 2),
            vwap=round(vwap, 2),
            ema_fast=round(ema_f, 2),
            ema_slow=round(ema_s, 2),
            meta_probability=round(meta_prob, 3),
            kalman_vel_atr=round(kalman_vel_atr, 3),
            hurst=round(hurst, 3),
        )
