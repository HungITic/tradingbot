import asyncio
from app.main import trading_service

async def main():
    ok = await trading_service.broker.connect()
    tick = await trading_service.broker.get_tick("XAUUSD")
    import time, datetime
    local_ts = time.time()
    if tick:
        print(f"Broker Time: {tick.time} ({datetime.datetime.fromtimestamp(tick.time)})")
    print(f"Local Time: {local_ts} ({datetime.datetime.fromtimestamp(local_ts)})")
    
if __name__ == "__main__":
    asyncio.run(main())
