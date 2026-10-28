"""Read-only IBKR paper preflight. Never submits, modifies, or cancels orders."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from ib_async import IB, Future


def require_paper(accounts, selected):
    if selected not in accounts or not selected.startswith('DU'):
        raise RuntimeError('An explicitly verified DU paper account is required')
    return selected


async def validate(host='127.0.0.1', port=7497, account='', observe_seconds=10):
    ib = IB()
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'mode': 'read_only', 'port': port, 'passed': False, 'broker_messages': []}
    ib.errorEvent += lambda req, code, message, contract=None: report['broker_messages'].append(
        {'request': req, 'code': code, 'message': message})
    try:
        await ib.connectAsync(host, port, clientId=191, timeout=10,
                              readonly=True, raiseSyncErrors=True)
        accounts = ib.managedAccounts()
        selected = account or (accounts[0] if len(accounts) == 1 else '')
        require_paper(accounts, selected)
        report['paper_account_verified'] = True
        positions = await asyncio.wait_for(ib.reqPositionsAsync(), 10)
        orders = await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), 10)
        report['account_positions'] = [
            {'symbol': p.contract.localSymbol, 'conId': p.contract.conId, 'quantity': p.position}
            for p in positions if p.account == selected and p.position]
        report['account_open_orders'] = [
            {'symbol': t.contract.localSymbol, 'conId': t.contract.conId,
             'orderId': t.order.orderId, 'status': t.orderStatus.status}
            for t in orders if t.order.account == selected]
        details = await asyncio.wait_for(ib.reqContractDetailsAsync(Future(symbol='MNQ', exchange='CME', currency='USD')), 15)
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        valid = sorted((d for d in details if d.contract.lastTradeDateOrContractMonth >= today),
                       key=lambda d: d.contract.lastTradeDateOrContractMonth)
        if not valid:
            raise RuntimeError('No active MNQ contract resolved')
        detail = valid[0]
        contract = detail.contract
        report['contract'] = {'symbol': contract.localSymbol, 'conId': contract.conId,
                              'expiry': contract.lastTradeDateOrContractMonth,
                              'min_tick': detail.minTick, 'multiplier': contract.multiplier}
        bars = await ib.reqHistoricalDataAsync(contract, endDateTime='', durationStr='2 D',
                    barSizeSetting='2 mins', whatToShow='TRADES', useRTH=True,
                    formatDate=2, keepUpToDate=True, timeout=20)
        updates = []
        bars.updateEvent += lambda values, new: updates.append({'new_bar': new, 'count': len(values)})
        ib.reqMarketDataType(1)
        ticker = ib.reqMktData(contract, '', False, False)
        await asyncio.sleep(observe_seconds)
        report['historical_bars'] = len(bars)
        report['latest_bar'] = str(bars[-1].date) if bars else None
        report['stream_updates'] = len(updates)
        report['stream_observed'] = bool(updates)
        import math
        report['quote'] = {name: (value if math.isfinite(value) else None)
                           for name in ('bid', 'ask', 'last') if isinstance(value := getattr(ticker, name), (int, float))}
        report['market_data_type'] = ticker.marketDataType
        report['latest_bar_age_seconds'] = (datetime.now(timezone.utc) - bars[-1].date).total_seconds() if bars else None
        report['data_fresh'] = bool(bars) and report['latest_bar_age_seconds'] < 300
        ib.cancelMktData(contract)
        ib.cancelHistoricalData(bars)
        report['account_flat'] = not report['account_positions'] and not report['account_open_orders']
        report['target_contract_flat'] = not any(p['conId'] == contract.conId for p in report['account_positions'] + report['account_open_orders'])
        report['passed'] = (bool(bars) and report['target_contract_flat'] and report['stream_observed']
                            and report['data_fresh'] and report['market_data_type'] == 1)
        report['order_lifecycle_validated'] = False
        report['next_step'] = 'Controlled paper bracket test; not performed by this read-only tool'
    except Exception as exc:
        report['failure'] = f'{type(exc).__name__}: {exc}'
    finally:
        ib.disconnect()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=7497)
    parser.add_argument('--account', default='')
    parser.add_argument('--observe-seconds', type=int, default=10)
    parser.add_argument('--output', default='paper_preflight.json')
    args = parser.parse_args()
    if args.port not in (7497, 4002):
        parser.error('Only standard paper ports 7497/4002 are allowed')
    if not 1 <= args.observe_seconds <= 60:
        parser.error('Observation must be 1–60 seconds')
    report = asyncio.run(validate(port=args.port, account=args.account, observe_seconds=args.observe_seconds))
    Path(args.output).write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
