import asyncio
from datetime import datetime, timedelta
from app.main import trading_service
from app.config.settings import settings

async def main():
    ok = await trading_service.broker.connect()
    print("MT5 Connected:", ok)
    
    past = datetime.now() - timedelta(hours=1)
    deals = await trading_service.broker.get_history_deals(start_time=past)
    print(f"Found {len(deals)} deals in the last 1 hour.")
    for d in deals:
        print(f"Deal {d.ticket}: entry={d.entry}, reason={d.reason}, profit={d.profit}, symbol={d.symbol}")

if __name__ == "__main__":
    asyncio.run(main())
