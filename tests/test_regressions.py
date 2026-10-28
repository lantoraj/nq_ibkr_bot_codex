import pandas as pd
from config import BotConfig
from indicators import compute_rsi, enrich_indicators
from risk_manager import RiskManager
from backtest import Backtester, generate_synthetic_nq_data
from ib_async import StopOrder

def test_rising_rsi():
    assert compute_rsi(pd.Series(range(30))).iloc[-1] == 100

def test_budget_cannot_afford_contract():
    c = BotConfig()
    c.risk.risk_per_trade_usd = 10
    assert RiskManager(c.risk, c.contract).calculate_contracts(40) == 0

def test_opening_range_unavailable_before_completion():
    df = generate_synthetic_nq_data(1)
    enriched = enrich_indicators(df)
    assert enriched.iloc[:30].orb_high.isna().all()
    assert enriched.iloc[30:].orb_high.notna().all()

def test_repeat_run_resets_risk():
    b = Backtester(BotConfig())
    df = generate_synthetic_nq_data(2)
    assert b.run(df)['metrics'] == b.run(df)['metrics']

def test_stop_price_field():
    stop = StopOrder('SELL', 1, 100)
    stop.auxPrice = 101
    assert stop.auxPrice == 101

def test_kalman_configuration_changes_features():
    df = generate_synthetic_nq_data(1)
    assert not enrich_indicators(df).kalman_price.equals(enrich_indicators(df, kalman_r_meas=100).kalman_price)
