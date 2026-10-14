import asyncio
import datetime
import time
from app.main import trading_service
from app.core.timezone import now_vietnam, format_vietnam_time, get_broker_utc_offset_hours

async def main():
    ok = await trading_service.broker.connect()
    tick = await trading_service.broker.get_tick("XAUUSD")
    local_ts = time.time()
    offset = get_broker_utc_offset_hours()
    print(f"MT5 Connected: {ok}")
    print(f"Broker UTC Offset: UTC+{offset}")
    if tick:
        print(f"Mapped Tick Time (VN): {tick.time} ({format_vietnam_time(tick.time, '%H:%M:%S %d/%m/%Y')})")
    print(f"Current Vietnam Time : {now_vietnam().strftime('%H:%M:%S %d/%m/%Y')} (ICT UTC+7)")
    print(f"Local System Time    : {datetime.datetime.fromtimestamp(local_ts).strftime('%H:%M:%S %d/%m/%Y')}")

if __name__ == "__main__":
    asyncio.run(main())
