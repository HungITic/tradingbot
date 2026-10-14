import asyncio
from datetime import datetime, timedelta
from app.main import trading_service
from app.config.settings import settings
from app.core.timezone import format_vietnam_time

async def main():
    ok = await trading_service.broker.connect()
    print("MT5 Connected:", ok)
    
    past = datetime.now() - timedelta(hours=1)
    deals = await trading_service.broker.get_history_deals(start_time=past)
    print(f"Found {len(deals)} deals in the last 1 hour.")
    for d in deals:
        time_str = format_vietnam_time(d.time, "%H:%M:%S %d/%m/%Y")
        print(f"Deal {d.ticket}: pos_id={d.position_id}, entry={d.entry}, reason={d.reason}, profit={d.profit}, symbol={d.symbol}, time={time_str}")

    positions = await trading_service.get_closed_positions(days=7)
    print(f"\nFound {len(positions)} aggregated positions in last 7 days:")
    for p in positions:
        print(f"Position #{p.ticket}: {p.symbol} {p.position_type} {p.volume} lot, Entry: {p.open_price} -> Exit: {p.close_price}, PnL: {p.net_pnl}, deals={p.deals_count}, fully_closed={p.is_fully_closed}")

if __name__ == "__main__":
    asyncio.run(main())
