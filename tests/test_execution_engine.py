"""Deterministic broker-event scenarios; no network or brokerage account required."""
import asyncio
import sqlite3
from types import SimpleNamespace as NS
import pytest
from config import BotConfig
from position_engine import PositionEngine
from ibkr_execution import IBKRNQBotRunner
from ib_async import Contract, StopOrder, OrderStatus, Trade, MarketOrder


class SimulatedBroker:
    def __init__(self, position=2, cancellation_fill=0):
        self.position = position
        self.cancellation_fill = cancellation_fill
        self.sent = []
        self.contract = Contract(conId=1)
        self.stop = Trade(self.contract, StopOrder('SELL', position, 90, account='paper', orderId=2), OrderStatus(status='Submitted'))

    def openTrades(self):
        return [self.stop] if not self.stop.isDone() else []

    def cancelOrder(self, order):
        self.position -= self.cancellation_fill
        self.stop.orderStatus.status = 'Cancelled'

    async def reqPositionsAsync(self):
        return [NS(account='paper', contract=self.contract, position=self.position)]

    def placeOrder(self, contract, order):
        self.sent.append(order)
        self.position += order.totalQuantity * (1 if order.action == 'BUY' else -1)
        return Trade(contract, order, OrderStatus(status='Filled'))


def engine(direction='LONG', qty=2):
    return PositionEngine(direction, qty, 90 if direction == 'LONG' else 110, 10, 0)


def protected(direction='LONG'):
    e = engine(direction)
    e.fill('entry', 2, 100, 1)
    e.acknowledge_stop(e.initial_stop, 2)
    return e


def test_partial_fill_requires_sufficient_protection_and_deduplicates():
    e = engine()
    e.fill('a', 1, 100, 1)
    assert e.state == 'UNPROTECTED'
    e.acknowledge_stop(90, 1)
    assert e.state == 'PROTECTED'
    assert not e.fill('a', 1, 100, 1)
    e.fill('b', 1, 102, 2)
    assert e.average == 101 and e.state == 'UNPROTECTED'
    e.acknowledge_stop(90, 2)
    e.fill('exit-a', 1, 110, 3, entry=False)
    assert e.remaining == 1
    e.fill('exit-b', 1, 110, 3, entry=False)
    assert e.state == 'CLOSED'


@pytest.mark.parametrize('direction,high,low,expected', [('LONG',111,99,101),('SHORT',101,89,99)])
def test_delayed_ack_does_not_advance_stop(direction, high, low, expected):
    e = protected(direction)
    original = e.stop
    assert e.propose_stop(high,low,2,BotConfig().risk) == expected
    assert e.stop == original and e.state == 'AMEND_PENDING'
    assert e.propose_stop(high,low,3,BotConfig().risk) is None
    e.acknowledge_stop(original,2)  # stale broker update
    assert e.pending_stop == expected
    e.acknowledge_stop(expected,2)
    assert e.stop_reason == 'Break-Even Stop' and e.state == 'PROTECTED'


def test_rejection_preserves_confirmed_protection_and_blocks():
    e=protected()
    e.propose_stop(111,99,2,BotConfig().risk)
    e.reject('rejected amendment')
    assert e.stop == 90 and e.state == 'BLOCKED'


def test_decay_independent_of_break_even_setting():
    e=protected()
    cfg=BotConfig().risk
    cfg.breakeven_at_1r=False
    assert e.propose_stop(104,96,7,cfg)==95


def test_cancel_race_late_entry_fill_remains_visible():
    e=engine()
    e.cancel_entry()
    e.fill('late',1,100,3)
    assert e.remaining == 1 and e.state == 'UNPROTECTED'


def test_restart_reconciliation_never_adopts_exposure():
    e=engine()
    assert not e.reconcile(1,1)
    assert 'Reconciliation' in e.blocked_reason
    e.cancel_entry()
    assert e.reconcile(0,0)


def runner():
    r=IBKRNQBotRunner(BotConfig())
    r.cfg.ibkr.account='paper'
    r.contract=Contract(conId=1)
    r.position_engine=protected()
    r._stop_order_id=2
    r._active_parent_order=MarketOrder('BUY',2,orderId=1)
    return r


def test_live_adapter_ack_and_disconnect():
    r=runner()
    e=r.position_engine
    e.propose_stop(111,99,2,r.cfg.risk)
    trade=Trade(r.contract,StopOrder('SELL',2,101,orderId=2),OrderStatus(status='PendingSubmit'))
    r._on_open_order(trade)
    assert e.stop==90
    trade.orderStatus.status='Submitted'
    r._on_open_order(trade)
    assert e.stop==101
    r._on_disconnect()
    assert r._blocked and e.state=='BLOCKED'


def test_live_stop_rejection_requests_flatten():
    r=runner()
    reasons=[]
    r.flatten_all=reasons.append
    r._on_order_error(2,201,'Rejected')
    assert reasons and r.position_engine.state=='BLOCKED'


def test_missing_protection_ack_times_out():
    r=runner()
    r._protection_deadline=0
    reasons=[]
    r.flatten_all=reasons.append
    calls=iter([True, False])
    r.ib=NS(isConnected=lambda: next(calls))
    original=asyncio.sleep
    async def immediate(_):
        return None
    # No market positions or session checks are needed for this timeout case.
    r.has_open_position_or_orders=lambda: False
    asyncio.sleep=immediate
    try:
        asyncio.run(r._watchdog())
    finally:
        asyncio.sleep=original
    assert reasons==['Protection acknowledgement timeout']


def test_lifecycle_persists_reconciliation_marker():
    r=runner()
    r._ledger=sqlite3.connect(':memory:')
    r._ledger.execute('CREATE TABLE state (id INTEGER PRIMARY KEY, data TEXT)')
    r._persist()
    import json
    data=json.loads(r._ledger.execute('SELECT data FROM lifecycle').fetchone()[0])
    assert data['remaining']==2 and data['state']=='PROTECTED'


@pytest.mark.parametrize('race_fill,expected_close',[(0,2),(1,1),(2,0)])
def test_flatten_reconciles_after_cancellation_race(race_fill, expected_close):
    r=runner()
    broker=SimulatedBroker(cancellation_fill=race_fill)
    r.ib=broker
    r._blocked=True
    asyncio.run(r._flatten_async('test'))
    assert broker.position==0
    assert sum(o.totalQuantity for o in broker.sent)==expected_close
    assert r._blocked


def test_commission_duplicate_does_not_count_twice():
    r=runner()
    r._ledger=sqlite3.connect(':memory:')
    r._ledger.execute('CREATE TABLE fills (id TEXT PRIMARY KEY, qty REAL, pnl REAL)')
    r._ledger.execute('CREATE TABLE state (id INTEGER PRIMARY KEY, data TEXT)')
    trade=NS(order=NS(account='paper',orderRef='nq_codex'),contract=r.contract)
    def send(id,side,pnl):
        fill=NS(execution=NS(execId=id,shares=1,side=side,time='2026-10-28T14:00:00Z'))
        r._on_commission_report(trade,fill,NS(realizedPNL=pnl,commission=.62))
    send('entry','BOT',1e308)
    send('exit','SLD',0)
    send('exit','SLD',0)
    assert r.risk.state.trades_taken==1
    assert r.risk.state.realized_pnl_usd==-.62
