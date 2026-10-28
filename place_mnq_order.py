"""
Place a 1-contract MNQ front-month Market BUY bracket order on IBKR TWS Paper Account
with a $2,000 Stop Loss (1,000 points) and $5,000 Take Profit (2,500 points).
"""
import math
from datetime import datetime
from ib_async import IB, Future, LimitOrder, MarketOrder, StopOrder


def round_to_tick(price: float, tick_size: float = 0.25) -> float:
    return round(round(price / tick_size) * tick_size, 2)


def main() -> None:
    ib = IB()
    host = "127.0.0.1"
    port = 7497  # TWS Paper Trading port
    client_id = 202

    print(f"Connecting to IBKR TWS Paper at {host}:{port} (clientId={client_id})...")
    ib.connect(host=host, port=port, clientId=client_id, timeout=20)
    print(f"Connected! Managed accounts: {ib.managedAccounts()}")

    try:
        # 1. Resolve current front-month MNQ contract on CME
        probe = Future(symbol="MNQ", exchange="CME", currency="USD")
        details = ib.reqContractDetails(probe)
        if not details:
            raise RuntimeError("No contract details returned for MNQ on CME.")

        today_str = datetime.now().strftime("%Y%m%d")
        valid_contracts = [
            d.contract for d in details if d.contract.lastTradeDateOrContractMonth >= today_str
        ]
        valid_contracts.sort(key=lambda c: c.lastTradeDateOrContractMonth)
        contract = valid_contracts[0]
        print(
            f"Resolved front-month contract: {contract.localSymbol} "
            f"(conId={contract.conId}, expiry={contract.lastTradeDateOrContractMonth}, multiplier={contract.multiplier})"
        )

        multiplier = float(contract.multiplier or 2.0)
        stop_loss_usd = 2000.0
        take_profit_usd = 5000.0

        stop_points = stop_loss_usd / multiplier      # 2000 / 2 = 1000.0 pts
        tp_points = take_profit_usd / multiplier      # 5000 / 2 = 2500.0 pts

        # 2. Fetch current market reference price (outside RTH included)
        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr="1800 S",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
            keepUpToDate=False,
        )
        if bars:
            ref_price = float(bars[-1].close)
            print(f"Latest MNQ trade price (from real-time 1m bars): {ref_price:.2f}")
        else:
            ib.reqMarketDataType(3)  # Fallback to delayed if needed
            tickers = ib.reqTickers(contract)
            t = tickers[0] if tickers else None
            ref_price = float(t.marketPrice()) if (t and not math.isnan(t.marketPrice())) else 0.0
            if ref_price <= 0:
                raise RuntimeError("Unable to determine current MNQ reference price.")
            print(f"Latest MNQ market price (from ticker): {ref_price:.2f}")

        sl_price = round_to_tick(ref_price - stop_points)
        tp_price = round_to_tick(ref_price + tp_points)

        print(
            f"Preparing Bracket Order: BUY 1 {contract.localSymbol} @ MKT (~{ref_price:.2f}) | "
            f"Stop Loss: {sl_price:.2f} (-{stop_points:.2f} pts / -${stop_loss_usd:.0f}) | "
            f"Take Profit: {tp_price:.2f} (+{tp_points:.2f} pts / +${take_profit_usd:.0f})"
        )

        # 3. Construct and place atomic Server-Side Bracket Order
        parent_id = ib.client.getReqId()
        tp_id = ib.client.getReqId()
        sl_id = ib.client.getReqId()

        parent = MarketOrder(
            action="BUY",
            totalQuantity=1,
            orderId=parent_id,
            transmit=False,
            outsideRth=True,
            tif="GTC",
        )

        take_profit = LimitOrder(
            action="SELL",
            totalQuantity=1,
            lmtPrice=tp_price,
            orderId=tp_id,
            parentId=parent_id,
            transmit=False,
            outsideRth=True,
            tif="GTC",
        )

        stop_loss = StopOrder(
            action="SELL",
            totalQuantity=1,
            stopPrice=sl_price,
            orderId=sl_id,
            parentId=parent_id,
            transmit=True,
            outsideRth=True,
            tif="GTC",
        )

        parent_trade = ib.placeOrder(contract, parent)
        tp_trade = ib.placeOrder(contract, take_profit)
        sl_trade = ib.placeOrder(contract, stop_loss)

        # Wait up to 10 seconds for order acknowledgment / fill
        for _ in range(20):
            ib.sleep(0.5)
            if parent_trade.orderStatus.status in ("Filled", "PreSubmitted", "Submitted"):
                if parent_trade.orderStatus.status == "Filled":
                    break

        fill_price = parent_trade.orderStatus.avgFillPrice or ref_price
        print(f"Parent Order (ID {parent_id}) Status: {parent_trade.orderStatus.status} | Avg Fill Price: {fill_price:.2f}")

        # If filled at a slightly different price than ref_price, adjust SL and TP to exact fill_price +/- points
        if parent_trade.orderStatus.status == "Filled" and fill_price > 0:
            exact_sl = round_to_tick(fill_price - stop_points)
            exact_tp = round_to_tick(fill_price + tp_points)
            if exact_sl != sl_price or exact_tp != tp_price:
                print(f"Adjusting bracket child orders to exact fill price {fill_price:.2f} -> SL={exact_sl:.2f}, TP={exact_tp:.2f}")
                take_profit.lmtPrice = exact_tp
                stop_loss.stopPrice = exact_sl
                ib.placeOrder(contract, take_profit)
                ib.placeOrder(contract, stop_loss)
                ib.sleep(1.0)

        print(f"Take Profit Order (ID {tp_id}) Status: {tp_trade.orderStatus.status} @ {take_profit.lmtPrice:.2f}")
        print(f"Stop Loss Order   (ID {sl_id}) Status: {sl_trade.orderStatus.status} @ {stop_loss.stopPrice:.2f}")

    finally:
        ib.disconnect()
        print("Disconnected from IBKR TWS.")


if __name__ == "__main__":
    main()
