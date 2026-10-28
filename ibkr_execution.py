"""
Interactive Brokers (TWS / IB Gateway) Execution & Live Streaming Engine using `ib_async`.
Handles:
- Connecting to TWS / IB Gateway (Paper or Live port)
- Resolving the active front-month NQ or MNQ futures contract on CME
- Streaming real-time historical + keepUpToDate bars
- Atomic placement of Server-Side Bracket Orders (Parent Limit/Market + Stop Loss + Take Profit)
- EOD position flattening & realized PnL tracking
"""
from __future__ import annotations

import logging
import asyncio
import sqlite3
import json
from dataclasses import asdict
from pathlib import Path
from datetime import datetime
from typing import List, Optional

import pandas as pd
from ib_async import IB, BarDataList, Contract, Future, LimitOrder, MarketOrder, Order, StopOrder, Trade, util

from config import BotConfig
from position_engine import PositionEngine
from copy import copy
from time import monotonic
from risk_manager import RiskManager
from strategy import NQIntradayStrategy, TradeSignal

logger = logging.getLogger("nq_ibkr_bot")


class IBKRNQBotRunner:
    """Live / Paper execution engine for NQ & MNQ futures on Interactive Brokers."""

    def __init__(self, config: BotConfig, mode: str = "paper") -> None:
        self.cfg = config
        self.mode = mode.lower()
        if self.mode not in ("paper", "live"):
            raise ValueError("Invalid execution mode")
        self._flattening = False
        self._blocked = False
        self._last_received = None
        self._ledger = None
        self.position_engine = None
        self._protection_deadline = None
        self._stop_order_id = None
        self.ib = IB()
        self.strategy = NQIntradayStrategy(config.strategy, config.risk, config.contract)
        self.risk = RiskManager(config.risk, config.contract)

        self.ib.openOrderEvent += self._on_open_order
        self.ib.errorEvent += self._on_order_error
        self.ib.disconnectedEvent += self._on_disconnect
        self.ib.execDetailsEvent += self._on_execution
        self.ib.orderStatusEvent += self._on_order_status
        self.contract: Optional[Contract] = None
        self.bars: Optional[BarDataList] = None
        self.bar_count: int = 0
        self._last_processed_bar_time: Optional[datetime] = None

        # Active bracket order tracking for +1R Break-Even, Time-Decay Stop & Unfilled Entry Timeout
        self._active_signal: Optional[TradeSignal] = None
        self._active_parent_order: Optional[Order] = None
        self._active_stop_order: Optional[Order] = None
        self._fill_price = None
        self._fill_bar = None
        self._entry_submitted_bar: int = -1
        self._be_moved: bool = False
        self._decay_moved: bool = False

    def _on_disconnect(self):
        self._blocked = True
        if self.position_engine:
            self.position_engine.reject("Disconnected; reconciliation required")

    def _on_order_status(self, trade):
        engine = self.position_engine
        if not engine:
            return
        if self._active_parent_order and trade.order.orderId == self._active_parent_order.orderId:
            if trade.orderStatus.status in ("Cancelled", "ApiCancelled", "Inactive", "Filled"):
                engine.cancel_entry()
        if trade.order.orderId == self._stop_order_id and trade.orderStatus.status in ("Cancelled", "ApiCancelled", "Inactive"):
            engine.protected_qty = 0
            if engine.remaining:
                engine.reject("Protective stop cancelled or rejected")
                self.flatten_all(engine.blocked_reason)

    def _on_execution(self, trade, fill):
        engine = self.position_engine
        if not engine or trade.order.account != self.cfg.ibkr.account or trade.order.orderRef != "nq_codex":
            return
        if trade.contract.conId != self.contract.conId:
            return
        entry = trade.order.orderId == self._active_parent_order.orderId
        engine.fill(fill.execution.execId, float(fill.execution.shares), float(fill.execution.price), self.bar_count, entry)
        if engine.state == "UNPROTECTED":
            if self._protection_deadline is None:
                self._protection_deadline = monotonic() + 10
            self.ib.client.reqOpenOrders()
        self._persist()

    def _on_open_order(self, trade):
        engine = self.position_engine
        if not engine or trade.order.orderId != self._stop_order_id:
            return
        if trade.orderStatus.status not in ("Submitted", "PreSubmitted"):
            return
        # A held child is not considered protection for a partially filled parent.
        if not engine.entry_terminal:
            return
        engine.acknowledge_stop(float(trade.order.auxPrice), float(trade.order.totalQuantity))
        if engine.state == "PROTECTED":
            self._protection_deadline = None
        self._persist()

    def _on_order_error(self, req_id, code, message, contract=None):
        if req_id == self._stop_order_id and code not in (2104, 2106, 2158):
            if self.position_engine:
                self.position_engine.reject(f"Stop error {code}: {message}")
            self.flatten_all(f"Stop error {code}: {message}")

    def connect(self) -> None:
        """Connect to IBKR TWS or IB Gateway."""
        port = self.cfg.ibkr.paper_port if self.mode == "paper" else self.cfg.ibkr.live_port
        logger.info(
            "Connecting to IBKR (%s mode) at %s:%d (clientId=%d)...",
            self.mode.upper(),
            self.cfg.ibkr.host,
            port,
            self.cfg.ibkr.client_id,
        )
        self.ib.connect(
            host=self.cfg.ibkr.host,
            port=port,
            clientId=self.cfg.ibkr.client_id,
            timeout=self.cfg.ibkr.timeout,
            account=self.cfg.ibkr.account or "",
        )
        accounts = self.ib.managedAccounts()
        if not self.cfg.ibkr.account:
            if len(accounts) != 1:
                raise RuntimeError("Configure an explicit account")
            self.cfg.ibkr.account = accounts[0]
        if self.cfg.ibkr.account not in accounts:
            raise RuntimeError("Configured account is unavailable")
        Path("state").mkdir(exist_ok=True)
        self._ledger = sqlite3.connect(f"state/{self.mode}_{self.cfg.ibkr.account}.sqlite")
        self._ledger.execute("CREATE TABLE IF NOT EXISTS fills (id TEXT PRIMARY KEY, qty REAL, pnl REAL)")
        self._ledger.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, data TEXT)")
        self._ledger.execute("CREATE TABLE IF NOT EXISTS lifecycle (id INTEGER PRIMARY KEY, data TEXT)")
        prior = self._ledger.execute("SELECT data FROM lifecycle WHERE id=1").fetchone()
        if prior and json.loads(prior[0])["state"] != "CLOSED":
            self._blocked = True
            raise RuntimeError("Unresolved persisted position lifecycle; reconcile before restart")
        saved = self._ledger.execute("SELECT data FROM state WHERE id=1").fetchone()
        if saved:
            from risk_manager import DailyRiskState
            from datetime import date
            values = json.loads(saved[0])
            if values["session_date"]:
                values["session_date"] = date.fromisoformat(values["session_date"])
            self.risk.state = DailyRiskState(**values)
            self.bar_count = max(0, self.risk.state.last_exit_bar_index)
        self.risk.check_and_reset_session(pd.Timestamp.now(tz=self.cfg.risk.timezone))

    def resolve_front_month_contract(self) -> Contract:
        """
        Qualify the NQ or MNQ futures contract.
        If `contract_month` is empty, automatically fetches all non-expired contracts
        from IBKR and selects the nearest upcoming quarterly expiry.
        """
        c_cfg = self.cfg.contract
        if c_cfg.contract_month:
            fut = Future(
                symbol=c_cfg.symbol,
                lastTradeDateOrContractMonth=c_cfg.contract_month,
                exchange=c_cfg.exchange,
                currency=c_cfg.currency,
            )
            qualified = self.ib.qualifyContracts(fut)
            if not qualified:
                raise RuntimeError(f"Could not qualify contract {c_cfg.symbol} {c_cfg.contract_month}")
            self.contract = qualified[0]
        else:
            probe = Future(symbol=c_cfg.symbol, exchange=c_cfg.exchange, currency=c_cfg.currency)
            details = self.ib.reqContractDetails(probe)
            if not details:
                raise RuntimeError(f"No contract details returned by IBKR for {c_cfg.symbol} on {c_cfg.exchange}")

            today_str = datetime.now().strftime("%Y%m%d")
            valid_contracts = [
                d.contract
                for d in details
                if d.contract.lastTradeDateOrContractMonth >= today_str
            ]
            valid_contracts.sort(key=lambda c: c.lastTradeDateOrContractMonth)
            if not valid_contracts:
                raise RuntimeError(f"No active non-expired contracts found for {c_cfg.symbol}")

            self.contract = valid_contracts[0]

        logger.info(
            "Qualified Active Contract: %s (localSymbol=%s, expiry=%s, conId=%s)",
            self.contract.symbol,
            self.contract.localSymbol,
            self.contract.lastTradeDateOrContractMonth,
            self.contract.conId,
        )
        return self.contract

    def has_open_position_or_orders(self) -> bool:
        """Check if there is an active position or open parent order on the target contract."""
        if self.contract is None:
            return False

        for pos in self.ib.positions():
            if pos.account == self.cfg.ibkr.account and pos.contract.conId == self.contract.conId and pos.position != 0:
                return True

        for trade in self.ib.openTrades():
            if trade.order.account == self.cfg.ibkr.account and trade.contract.conId == self.contract.conId:
                return True

        return False

    def flatten_all(self, reason: str = "Manual/EOD Flatten") -> None:
        """Cancel all open orders for the contract and close any open position with a MarketOrder."""
        if self.contract is None:
            return

        if not self._flattening:
            self._flattening = True
            self._blocked = True
            asyncio.ensure_future(self._flatten_async(reason))

    async def _flatten_async(self, reason):
        try:
            orders = [t for t in self.ib.openTrades() if t.order.account == self.cfg.ibkr.account
                      and t.contract.conId == self.contract.conId]
            for trade in orders:
                self.ib.cancelOrder(trade.order)
            for _ in range(100):
                if all(t.isDone() for t in orders):
                    break
                await asyncio.sleep(.1)
            else:
                raise RuntimeError("Cancellation unconfirmed; manual reconciliation required")
            positions = await self.ib.reqPositionsAsync()
            for pos in positions:
                if pos.account == self.cfg.ibkr.account and pos.contract.conId == self.contract.conId and pos.position:
                    order = MarketOrder("SELL" if pos.position > 0 else "BUY", abs(pos.position),
                                        account=pos.account, orderRef="nq_codex", tif="DAY")
                    trade = self.ib.placeOrder(self.contract, order)
                    for _ in range(100):
                        if trade.isDone():
                            break
                        await asyncio.sleep(.1)
                    if trade.orderStatus.status != "Filled":
                        raise RuntimeError("Flatten fill unconfirmed")
            positions = await self.ib.reqPositionsAsync()
            if any(p.account == self.cfg.ibkr.account and p.contract.conId == self.contract.conId and p.position for p in positions):
                raise RuntimeError("Exposure remains after flatten")
            logger.warning("Flatten verified: %s; entries remain blocked", reason)
        except Exception:
            logger.exception("Flatten failed; operator intervention required")
        finally:
            self._flattening = False

    def _persist(self):
        if self._ledger:
            self._ledger.execute("CREATE TABLE IF NOT EXISTS lifecycle (id INTEGER PRIMARY KEY, data TEXT)")
            if self.position_engine:
                engine = self.position_engine
                self._ledger.execute("INSERT OR REPLACE INTO lifecycle VALUES (1, ?)",
                                     (json.dumps({"state": engine.state, "remaining": engine.remaining,
                                                  "reason": engine.blocked_reason}),))
            self._ledger.execute("INSERT OR REPLACE INTO state VALUES (1, ?)",
                                 (json.dumps(asdict(self.risk.state), default=str),))
            self._ledger.commit()

    async def _watchdog(self):
        while self.ib.isConnected():
            if self._protection_deadline is not None and monotonic() >= self._protection_deadline:
                self._protection_deadline = None
                self.flatten_all("Protection acknowledgement timeout")
            now = pd.Timestamp.now(tz=self.cfg.risk.timezone)
            if self.risk.should_flatten_eod(now) and self.has_open_position_or_orders():
                self.flatten_all("Wall-clock EOD")
            if self._last_received and (now - self._last_received).total_seconds() > 600:
                self._blocked = True
                logger.error("Feed stale; new entries blocked")
            await asyncio.sleep(5)

    def place_bracket_order(
        self,
        signal: TradeSignal,
        quantity: int,
        use_market_entry: bool = False,
    ) -> List[Trade]:
        """
        Submit an atomic Server-Side Bracket Order to IBKR:
        - Parent Order: Limit (or Market) entry
        - Child 1: Take Profit LimitOrder
        - Child 2: Stop Loss StopOrder
        """
        if self.contract is None:
            raise RuntimeError("Contract is not resolved.")

        action = "BUY" if signal.direction == "LONG" else "SELL"
        reverse_action = "SELL" if action == "BUY" else "BUY"

        parent_id = self.ib.client.getReqId()

        if use_market_entry:
            parent: Order = MarketOrder(
                action=action,
                totalQuantity=quantity,
                orderId=parent_id,
                transmit=False,
                outsideRth=not self.cfg.risk.rth_only,
                tif="DAY",
            )
        else:
            from indicators import round_to_tick

            slip_pts = self.cfg.risk.entry_limit_slippage_ticks * self.cfg.contract.tick_size
            lmt_price = (
                round_to_tick(signal.entry_price + slip_pts, self.cfg.contract.tick_size)
                if action == "BUY"
                else round_to_tick(signal.entry_price - slip_pts, self.cfg.contract.tick_size)
            )
            parent = LimitOrder(
                action=action,
                totalQuantity=quantity,
                lmtPrice=lmt_price,
                orderId=parent_id,
                transmit=False,
                outsideRth=not self.cfg.risk.rth_only,
                tif="DAY",
            )

        take_profit = LimitOrder(
            action=reverse_action,
            totalQuantity=quantity,
            lmtPrice=signal.take_profit,
            parentId=parent_id,
            transmit=False,
            outsideRth=not self.cfg.risk.rth_only,
            tif="DAY",
        )
        take_profit.orderId = self.ib.client.getReqId()

        stop_loss = StopOrder(
            action=reverse_action,
            totalQuantity=quantity,
            stopPrice=signal.stop_loss,
            parentId=parent_id,
            transmit=True,  # Final child order transmits the entire bracket atomically
            outsideRth=not self.cfg.risk.rth_only,
            tif="DAY",
        )
        stop_loss.orderId = self.ib.client.getReqId()

        if self._blocked or self._flattening:
            raise RuntimeError("Entries blocked pending reconciliation")
        self.position_engine = PositionEngine(signal.direction, quantity, signal.stop_loss,
                                               signal.stop_distance_points, self.bar_count, self.cfg.contract.tick_size)
        self._active_parent_order = parent
        self._active_stop_order = stop_loss
        self._stop_order_id = stop_loss.orderId
        self._persist()
        trades: List[Trade] = []
        for o in (parent, take_profit, stop_loss):
            o.account = self.cfg.ibkr.account
            o.orderRef = "nq_codex"
            trades.append(self.ib.placeOrder(self.contract, o))

        self._fill_price = None
        self._fill_bar = None
        def entry_fill(trade, fill):
            self._fill_price = float(trade.orderStatus.avgFillPrice or fill.execution.price)
            if self._fill_bar is None:
                self._fill_bar = self.bar_count
        trades[0].fillEvent += entry_fill
        self._active_signal = signal
        self._active_parent_order = parent
        self._active_stop_order = stop_loss
        self._entry_submitted_bar = self.bar_count
        self._be_moved = False
        self._decay_moved = False

        logger.info(
            "BRACKET ORDER SUBMITTED [%s] %s %d %s @ %.2f | SL=%.2f | TP=%.2f | Reason: %s",
            signal.direction,
            action,
            quantity,
            self.contract.localSymbol,
            signal.entry_price,
            signal.stop_loss,
            signal.take_profit,
            signal.reason,
        )
        return trades

    def _bars_to_dataframe(self, bars: BarDataList) -> pd.DataFrame:
        """Convert IBKR BarDataList to standardized OHLCV DataFrame (excluding incomplete forming bar)."""
        df = util.df(bars)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"date": "timestamp"})
        # When keepUpToDate=True, the last bar in `bars` is the currently forming bar;
        # evaluate on `df.iloc[:-1]` so signals only trigger on closed bars.
        if len(df) > 1:
            df = df.iloc[:-1].copy()
        return df[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)

    def _bar_is_fresh(self, timestamp, now=None):
        """Arrival time alone cannot distinguish delayed data from live data."""
        timestamp = pd.Timestamp(timestamp)
        if timestamp.tzinfo is None:
            return False
        now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
        seconds = {"1 min": 60, "2 mins": 120, "5 mins": 300}[self.cfg.strategy.bar_size]
        age = (now - timestamp).total_seconds()
        return 0 <= age <= seconds + 90

    def _on_bar_update(self, bars: BarDataList, has_new_bar: bool) -> None:
        """Callback triggered by `ib_async` whenever a new bar forms or updates."""
        self._last_received = pd.Timestamp.now(tz=self.cfg.risk.timezone)
        if not has_new_bar:
            return

        df = self._bars_to_dataframe(bars)
        if df.empty:
            return

        latest_closed_ts = pd.Timestamp(df.iloc[-1]["timestamp"])
        if not self._bar_is_fresh(latest_closed_ts):
            self._blocked = True
            logger.error("Stale or ambiguous bar timestamp; new entries blocked: %s", latest_closed_ts)
            return
        if self._last_processed_bar_time == latest_closed_ts:
            return
        self._last_processed_bar_time = latest_closed_ts
        self.bar_count += 1

        # 1. Check mandatory EOD Flatten
        if self.risk.should_flatten_eod(latest_closed_ts):
            if self.has_open_position_or_orders():
                self.flatten_all(reason="End of Day (15:55 ET) Auto-Flatten")
            return

        # 2. If already in a position or working order, check Unfilled Timeout & +1R Break-Even Stop
        if self.has_open_position_or_orders():
            has_pos = any(
                pos.account == self.cfg.ibkr.account and pos.contract.conId == self.contract.conId and pos.position != 0
                for pos in self.ib.positions()
            )
            if (
                self._active_parent_order is not None
                and self.position_engine is not None
                and not self.position_engine.entry_terminal
                and self.cfg.risk.cancel_unfilled_entry_bars > 0
                and (self.bar_count - self._entry_submitted_bar) >= self.cfg.risk.cancel_unfilled_entry_bars
            ):
                self.flatten_all(reason=f"Unfilled Entry Order Timeout ({self.cfg.risk.cancel_unfilled_entry_bars} bars)")
                self._active_parent_order = None
                self._active_signal = None
                return

            engine = self.position_engine
            if has_pos and engine and self._active_stop_order and not self._flattening:
                latest_bar = df.iloc[-1]
                proposed = engine.propose_stop(float(latest_bar.high), float(latest_bar.low), self.bar_count, self.cfg.risk)
                if proposed is not None:
                    amendment = copy(self._active_stop_order)
                    amendment.auxPrice = proposed
                    self._protection_deadline = monotonic() + 10
                    try:
                        self.ib.placeOrder(self.contract, amendment)
                    except Exception:
                        engine.reject("Stop amendment submission failed")
                        self.flatten_all(engine.blocked_reason)
            return

        if self._blocked or self._flattening:
            return
        # 3. Check intraday risk guardrails
        allowed, risk_reason = self.risk.can_open_trade(latest_closed_ts, self.bar_count)
        if not allowed:
            logger.debug("Skipping bar %s: %s", latest_closed_ts, risk_reason)
            return

        # 4. Compute indicators & evaluate strategy on latest closed bar
        enriched = self.strategy.prepare_dataframe(df)
        signal = self.strategy.evaluate_bar(enriched, idx=-1)
        if signal is not None:
            qty = self.risk.calculate_contracts(signal.stop_distance_points)
            if qty > 0:
                self.place_bracket_order(signal, quantity=qty)

    def _on_commission_report(self, trade: Trade, fill, report) -> None:
        """Track realized PnL from IBKR commission reports as positions close."""
        if (trade.order.account != self.cfg.ibkr.account or trade.order.orderRef != "nq_codex"
                or trade.contract.conId != self.contract.conId):
            return
        execution = fill.execution
        qty = float(execution.shares) * (1 if execution.side == "BOT" else -1)
        pnl = float(report.realizedPNL)
        if not (-1e7 < pnl < 1e7):
            pnl = -float(report.commission)
        inserted = self._ledger.execute("INSERT OR IGNORE INTO fills VALUES (?, ?, ?)",
                                        (execution.execId, qty, pnl)).rowcount
        if not inserted:
            return
        quantity, total = self._ledger.execute("SELECT SUM(qty), SUM(pnl) FROM fills").fetchone()
        # Retain IDs forever for deduplication; settled fills contribute zero.
        if abs(quantity or 0) < 1e-9:
            self.risk.check_and_reset_session(pd.Timestamp(execution.time))
            self.risk.record_trade_closed(total or 0, self.bar_count)
            self._ledger.execute("UPDATE fills SET qty=0, pnl=0")
        self._persist()
        if self.risk.state.halted:
            self.flatten_all(self.risk.state.halt_reason)

    def run_streaming(self) -> None:
        """Start real-time bar subscription and run the IBKR event loop."""
        self.connect()
        try:
            self.resolve_front_month_contract()
            if self.has_open_position_or_orders():
                raise RuntimeError("Existing exposure/orders: reconcile manually before starting")
            if self._ledger.execute("SELECT COUNT(*) FROM fills WHERE qty != 0").fetchone()[0]:
                raise RuntimeError("Unsettled execution ledger: reconcile before starting")
            self.ib.commissionReportEvent += self._on_commission_report

            logger.info(
                "Subscribing to %s real-time bars (%s, RTH=%s)...",
                self.contract.localSymbol,
                self.cfg.strategy.bar_size,
                self.cfg.risk.rth_only,
            )
            self.bars = self.ib.reqHistoricalData(
                self.contract,
                endDateTime="",
                durationStr=self.cfg.strategy.history_duration,
                barSizeSetting=self.cfg.strategy.bar_size,
                whatToShow="TRADES",
                useRTH=self.cfg.risk.rth_only,
                formatDate=1,
                keepUpToDate=True,
            )
            self.bars.updateEvent += self._on_bar_update
            logger.info("Bot is now live and listening for bar closes. Press Ctrl+C to stop.")
            watchdog = asyncio.ensure_future(self._watchdog())
            try:
                self.ib.run()
            finally:
                watchdog.cancel()
                self._persist()
        except KeyboardInterrupt:
            logger.info("Shutdown requested by user (Ctrl+C).")
        finally:
            if self.ib.isConnected():
                self.ib.disconnect()
                logger.info("Disconnected from IBKR.")

    def download_historical_csv(self, output_path: str, duration: str = "5 D") -> pd.DataFrame:
        """Download historical 1m/2m/5m NQ/MNQ bars from IBKR (using ContFuture for multi-month spans) and save to CSV."""
        from ib_async import ContFuture

        self.connect()
        try:
            # Use Continuous Future (ContFuture) for multi-week / multi-month historical downloads
            # so contract rollovers (e.g., Sep U6 -> Dec Z6) are stitched seamlessly by IBKR.
            if any(unit in duration.upper() for unit in ("M", "W")) or (
                "D" in duration.upper() and int(duration.split()[0]) > 15
            ):
                cont = ContFuture(
                    symbol=self.cfg.contract.symbol,
                    exchange=self.cfg.contract.exchange,
                    currency=self.cfg.contract.currency,
                )
                qualified = self.ib.qualifyContracts(cont)
                target_contract = qualified[0] if qualified else self.resolve_front_month_contract()
                logger.info("Using Continuous Future (%s) for multi-month history download (%s)...", target_contract.localSymbol, duration)
            else:
                target_contract = self.resolve_front_month_contract()

            bars = self.ib.reqHistoricalData(
                target_contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=self.cfg.strategy.bar_size,
                whatToShow="TRADES",
                useRTH=self.cfg.risk.rth_only,
                formatDate=1,
                keepUpToDate=False,
                timeout=120,
            )
            df = util.df(bars)
            if df is None or df.empty:
                raise RuntimeError("No historical bars returned from IBKR.")
            df = df.rename(columns={"date": "timestamp"})
            df = df[["timestamp", "open", "high", "low", "close", "volume"]]
            df.to_csv(output_path, index=False)
            logger.info("Saved %d historical bars to %s", len(df), output_path)
            return df
        finally:
            if self.ib.isConnected():
                self.ib.disconnect()
