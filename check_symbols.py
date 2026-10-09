import mt5_mac as mt5
import os
from dotenv import load_dotenv

load_dotenv()
server = os.getenv("MT5_SERVER")
login = int(os.getenv("MT5_LOGIN"))
password = os.getenv("MT5_PASSWORD")

if not mt5.initialize(server=server, login=login, password=password):
    print("Khong the ket noi MT5, loi:", mt5.last_error())
    quit()

symbols = mt5.symbols_get()
if symbols is None:
    print("Khong the lay danh sach symbol")
else:
    print("Tim thay cac Symbol chua XAU / GOLD:")
    for s in symbols:
        if "XAU" in s.name.upper() or "GOLD" in s.name.upper():
            print(f"- {s.name}")

mt5.shutdown()
