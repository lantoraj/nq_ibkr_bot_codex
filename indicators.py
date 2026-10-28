"""
Vectorized technical & quantitative indicators for intraday NQ futures trading (v4 Quant Engine):
- 1D Constant-Velocity Kalman Filter (State-Space Position & Instantaneous Velocity Estimator)
- Rolling Variance Ratio (VR) & Implied Hurst Exponent (H) Regime Persistence Estimator
- Garman-Klass High-Efficiency OHLC Volatility Estimator
- Session-anchored VWAP & Volume-Weighted Standard Deviation Bands (VWAP Z-Score)
- Prior Day High (PDH) & Prior Day Low (PDL) structural levels
- Opening Range (09:30 - 09:59 ET) High & Low per session
- Exponential Moving Averages (Fast, Slow, Trend) & Normalized Slope
- Candle Body Ratio & Upper/Lower Wick Rejection Ratios
- Average True Range (Wilder's / EMA smoothing) & Wilder's RSI
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def round_to_tick(price: float, tick_size: float = 0.25) -> float:
    """Round a raw price to the nearest valid futures tick increment (0.25 for NQ/MNQ)."""
    return round(round(price / tick_size) * tick_size, 2)


def compute_kalman_state(
    series: pd.Series,
    q_pos: float = 0.05,
    q_vel: float = 0.01,
    r_meas: float = 1.0,
) -> tuple[pd.Series, pd.Series]:
    """
    1D Constant-Velocity Kalman Filter on close price:
      State vector: x_t = [position_t, velocity_t]^T
      State transition matrix: F = [[1, 1], [0, 1]]
      Measurement matrix: H = [1, 0]
    Returns (kalman_price, kalman_velocity).
    """
    y = series.to_numpy(dtype=float)
    n = len(y)
    if n == 0:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    k_price = np.zeros(n, dtype=float)
    k_vel = np.zeros(n, dtype=float)

    x0 = y[0]
    x1 = 0.0
    p00, p01, p10, p11 = 1.0, 0.0, 0.0, 1.0

    for i in range(n):
        # Prediction step
        x0_pred = x0 + x1
        x1_pred = x1

        p00_pred = p00 + p01 + p10 + p11 + q_pos
        p01_pred = p01 + p11
        p10_pred = p10 + p11
        p11_pred = p11 + q_vel

        # Innovation / Measurement update step
        err = y[i] - x0_pred
        s = p00_pred + r_meas
        k0 = p00_pred / s
        k1 = p10_pred / s

        x0 = x0_pred + k0 * err
        x1 = x1_pred + k1 * err

        p00 = (1.0 - k0) * p00_pred
        p01 = (1.0 - k0) * p01_pred
        p10 = p10_pred - k1 * p00_pred
        p11 = p11_pred - k1 * p01_pred

        k_price[i] = x0
        k_vel[i] = x1

    return pd.Series(k_price, index=series.index), pd.Series(k_vel, index=series.index)


def compute_variance_ratio_hurst(series: pd.Series, q: int = 4, window: int = 24) -> pd.Series:
    """
    Compute rolling Variance Ratio VR(q) and implied Hurst Exponent H:
      VR(q) = Var(r_t(q)) / (q * Var(r_t(1)))
      H = 0.5 * (1 + ln(VR(q)) / ln(q))
    H > 0.5 indicates positive autocorrelation (trending); H < 0.5 indicates mean reversion.
    """
    if series.empty:
        return pd.Series(dtype=float)

    r1 = series.diff(1)
    rq = series.diff(q)

    var1 = r1.rolling(window, min_periods=q + 2).var()
    varq = rq.rolling(window, min_periods=q + 2).var()

    vr = (varq / (q * var1.replace(0, np.nan))).clip(lower=0.1, upper=4.0).fillna(1.0)
    hurst = 0.5 * (1.0 + np.log(vr) / np.log(q))
    return hurst.clip(0.1, 0.9)


def compute_garman_klass_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Compute Garman-Klass high-efficiency OHLC volatility estimator scaled to index points.
    """
    if df.empty:
        return pd.Series(dtype=float)

    h = df["high"].clip(lower=1.0)
    l = df["low"].clip(lower=1.0)
    o = df["open"].clip(lower=1.0)
    c = df["close"].clip(lower=1.0)

    log_hl = np.log(h / l)
    log_co = np.log(c / o)
    gk_var = np.maximum(0.0, 0.5 * (log_hl ** 2) - (2.0 * np.log(2.0) - 1.0) * (log_co ** 2))
    gk_sigma_pts = np.sqrt(gk_var.ewm(alpha=1.0 / period, adjust=False).mean()) * c
    return gk_sigma_pts * 1.35


def compute_session_vwap(df: pd.DataFrame, timezone: str = "America/New_York") -> pd.Series:
    """
    Calculate Session-Anchored VWAP that resets at the start of each calendar day
    in US Eastern Time (or per date if timestamps are naive).
    """
    if df.empty:
        return pd.Series(dtype=float)

    timestamps = pd.to_datetime(df["timestamp"], utc=True)
    local_dates = timestamps.dt.tz_convert(timezone).dt.date

    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["volume"].replace(0, 1)
    tp_vol = typical_price * volume

    cum_tp_vol = tp_vol.groupby(local_dates).cumsum()
    cum_vol = volume.groupby(local_dates).cumsum()

    return cum_tp_vol / cum_vol


def compute_session_vwap_zscore(
    df: pd.DataFrame,
    vwap_series: pd.Series,
    timezone: str = "America/New_York",
) -> pd.Series:
    """
    Calculate how many volume-weighted standard deviations (Z-score) the current Close is
    from the Session VWAP.
    """
    if df.empty:
        return pd.Series(dtype=float)

    timestamps = pd.to_datetime(df["timestamp"], utc=True)
    local_dates = timestamps.dt.tz_convert(timezone).dt.date

    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["volume"].replace(0, 1)

    cum_vol = volume.groupby(local_dates).cumsum()
    cum_tp2_vol = ((typical_price ** 2) * volume).groupby(local_dates).cumsum()

    vwap_var = (cum_tp2_vol / cum_vol) - (vwap_series ** 2)
    vwap_std = np.sqrt(np.maximum(0.0, vwap_var)).replace(0.0, 1.0)

    return (df["close"] - vwap_series) / vwap_std


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """Compute standard Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute Average True Range using Wilder's smoothing (alpha = 1/period)."""
    if df.empty:
        return pd.Series(dtype=float)

    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Compute Wilder's Relative Strength Index (0 to 100)."""
    if series.empty:
        return pd.Series(dtype=float)

    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.mask((avg_loss == 0) & (avg_gain > 0), 100.0).fillna(50.0)


def enrich_indicators(
    df: pd.DataFrame,
    fast_ema: int = 9,
    slow_ema: int = 21,
    trend_ema: int = 50,
    atr_period: int = 14,
    rsi_period: int = 14,
    timezone: str = "America/New_York",
    kalman_q_pos: float = 0.05, kalman_q_vel: float = 0.01,
    kalman_r_meas: float = 1.0, hurst_lag_q: int = 4, hurst_window: int = 24,
) -> pd.DataFrame:
    """
    Compute and attach all technical and quantitative v4 indicators to an OHLCV DataFrame.
    Expected columns: ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    """
    out = df.copy()
    out["ema_fast"] = compute_ema(out["close"], fast_ema)
    out["ema_slow"] = compute_ema(out["close"], slow_ema)
    out["ema_trend"] = compute_ema(out["close"], trend_ema)
    out["vwap"] = compute_session_vwap(out, timezone=timezone)
    out["vwap_z"] = compute_session_vwap_zscore(out, out["vwap"], timezone=timezone)
    out["atr"] = compute_atr(out, period=atr_period)
    out["gk_atr"] = compute_garman_klass_atr(out, period=atr_period)
    out["rsi"] = compute_rsi(out["close"], period=rsi_period)

    atr_safe = out["atr"].replace(0, 1.0)
    out["ema_spread_atr"] = (out["ema_fast"] - out["ema_slow"]).abs() / atr_safe
    out["slow_slope_atr"] = (out["ema_slow"] - out["ema_slow"].shift(3)).fillna(0.0) / atr_safe

    # v4 Quant State-Space Kalman Velocity & Variance-Ratio Hurst Exponent
    k_price, k_vel = compute_kalman_state(out["close"], kalman_q_pos, kalman_q_vel, kalman_r_meas)
    out["kalman_price"] = k_price
    out["kalman_vel_atr"] = k_vel / atr_safe
    out["hurst"] = compute_variance_ratio_hurst(out["close"], q=hurst_lag_q, window=hurst_window)

    bar_range = (out["high"] - out["low"]).replace(0, 0.25)
    out["body_ratio"] = (out["close"] - out["open"]).abs() / bar_range
    out["upper_wick_ratio"] = (out["high"] - np.maximum(out["open"], out["close"])) / bar_range
    out["lower_wick_ratio"] = (np.minimum(out["open"], out["close"]) - out["low"]) / bar_range

    vol_ma = out["volume"].rolling(window=20, min_periods=1).mean().replace(0, 1)
    out["vol_ratio"] = out["volume"] / vol_ma

    # Rolling 3-bar high/low for momentum continuation triggers
    out["prev_3_high"] = out["high"].shift(1).rolling(3, min_periods=1).max()
    out["prev_3_low"] = out["low"].shift(1).rolling(3, min_periods=1).min()

    # Opening Range (09:30 - 09:59 ET) High & Low + Prior Day High/Low (PDH/PDL) per trading day
    ts_et = pd.to_datetime(out["timestamp"], utc=True).dt.tz_convert(timezone)
    dates = ts_et.dt.date
    hhmms = ts_et.dt.strftime("%H:%M")

    daily_highs = out["high"].groupby(dates).max().shift(1)
    daily_lows = out["low"].groupby(dates).min().shift(1)
    out["pdh"] = dates.map(daily_highs)
    out["pdl"] = dates.map(daily_lows)

    orb_mask = (hhmms >= "09:30") & (hhmms < "10:00")
    if orb_mask.any():
        orb_highs = out["high"][orb_mask].groupby(dates[orb_mask]).max()
        orb_lows = out["low"][orb_mask].groupby(dates[orb_mask]).min()
        out["orb_high"] = dates.map(orb_highs).where(hhmms >= "10:00")
        out["orb_low"] = dates.map(orb_lows).where(hhmms >= "10:00")
    else:
        out["orb_high"] = np.nan
        out["orb_low"] = np.nan

    return out
