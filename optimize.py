"""Small chronological parameter study using the production backtester.
Validation is descriptive: the supplied historical period was previously tuned.
"""
from copy import deepcopy
from itertools import product
import pandas as pd
from config import BotConfig
from backtest import Backtester

def main():
    df = pd.read_csv("data/mnq_2m_3months.csv")
    days = pd.to_datetime(df.timestamp, utc=True).dt.date
    cutoff = sorted(days.unique())[int(len(days.unique()) * .7)]
    train, validation = df[days < cutoff], df[days >= cutoff]
    results = []
    for atr, rr in product([1.0, 1.2, 1.5], [2.0, 2.5, 3.0]):
        cfg = BotConfig()
        cfg.risk.atr_stop_multiplier, cfg.risk.reward_risk_ratio = atr, rr
        metrics = Backtester(cfg).run(train)["metrics"]
        results.append((metrics["net_pnl_usd"], cfg))
    score, best = max(results, key=lambda item: item[0])
    print("Training-selected ATR / RR:", best.risk.atr_stop_multiplier, best.risk.reward_risk_ratio)
    print("Training net:", score)
    print("Chronological validation (not untouched holdout):", Backtester(best).run(validation)["metrics"])

if __name__ == "__main__":
    main()
