"""
Phase 0 — MT5 Bridge Verification Script
==========================================
Run this script to verify Python ↔ MT5 ↔ Exness Demo works on your Mac.

Usage:
    python scripts/test_mt5_bridge.py
"""

import sys
import time
from datetime import datetime
from typing import Any


def print_header(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def print_test(num: int, name: str):
    print(f"\n--- TEST {num}: {name} ---")


def test_import():
    """Test MetaTrader5 or mt5_mac package can be imported."""
    print_test(0, "Import MetaTrader5 / mt5_mac package")
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
        print(f"  ✅ Native MetaTrader5 package loaded (version: {getattr(mt5, '__version__', 'unknown')})")
        return mt5
    except ImportError:
        try:
            import mt5_mac as mt5  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
            print(f"  ✅ mt5_mac bridge loaded for Apple Silicon (version: {getattr(mt5, '__version__', 'unknown')})")
            return mt5
        except ImportError as e:
            print(f"  ❌ Import failed: {e}")
            print("  💡 Try: pip install mt5_mac")
            return None
    except Exception as e:
        print(f"  ❌ Unexpected error: {e}")
        return None


import os


def load_env() -> dict[str, str]:
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    env = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
    return env


def test_initialize(mt5, env: dict[str, str]):
    """Test MT5 terminal connection via IPC."""
    print_test(1, "mt5.initialize()")

    init_kwargs: dict[str, Any] = {"portable": True}
    if env.get("MT5_LOGIN") and env.get("MT5_LOGIN") != "0":
        try:
            init_kwargs["login"] = int(env["MT5_LOGIN"])
        except ValueError:
            pass
    if env.get("MT5_PASSWORD"):
        init_kwargs["password"] = env["MT5_PASSWORD"]
    if env.get("MT5_SERVER"):
        init_kwargs["server"] = env["MT5_SERVER"]
    if env.get("MT5_PATH"):
        init_kwargs["path"] = env["MT5_PATH"]

    if "login" in init_kwargs:
        print(f"  🔑 Using credentials from .env: Login={init_kwargs.get('login')}, Server={init_kwargs.get('server')}")
    else:
        print("  ℹ️ No credentials in .env, connecting to current active MT5 session...")

    start = time.time()
    ok = mt5.initialize(**init_kwargs)
    elapsed = (time.time() - start) * 1000

    if ok:
        print(f"  ✅ Initialized successfully ({elapsed:.0f}ms)")
        return True
    else:
        error = mt5.last_error()
        print(f"  ❌ Initialize failed ({elapsed:.0f}ms)")
        if isinstance(error, (list, tuple)):
            print(f"  Error code: {error[0]}")
            print(f"  Error message: {error[1]}")
        else:
            print(f"  Error code: {error}")
        print()
        print("  💡 Troubleshooting:")
        print("     1. Copy .env.example to .env and configure MT5_LOGIN, MT5_PASSWORD, MT5_SERVER")
        print("     2. In MT5 GUI, go to File -> Login to Trade Account and login with Exness Demo")
        print("     3. In MT5 GUI, go to Tools -> Options -> Expert Advisors -> check 'Allow algorithmic trading'")
        return False


def test_terminal_info(mt5):
    """Get MT5 terminal information."""
    print_test(2, "mt5.terminal_info()")

    info = mt5.terminal_info()
    if info is None:
        print(f"  ❌ Failed: {mt5.last_error()}")
        return False

    print(f"  Company:    {info.company}")
    print(f"  Name:       {info.name}")
    print(f"  Build:      {info.build}")
    print(f"  Connected:  {info.connected}")
    print(f"  Trade OK:   {info.trade_allowed}")
    print(f"  Path:       {info.path}")
    print(f"  ✅ Terminal info OK")
    return True


def test_account_info(mt5):
    """Get account information from Exness."""
    print_test(3, "mt5.account_info()")

    acc = mt5.account_info()
    if acc is None:
        print(f"  ❌ Failed: {mt5.last_error()}")
        return False

    print(f"  Login:      {acc.login}")
    print(f"  Server:     {acc.server}")
    print(f"  Name:       {acc.name}")
    print(f"  Balance:    {acc.balance} {acc.currency}")
    print(f"  Equity:     {acc.equity} {acc.currency}")
    print(f"  Margin:     {acc.margin}")
    print(f"  Free:       {acc.margin_free}")
    print(f"  Leverage:   1:{acc.leverage}")
    print(f"  Trade mode: {acc.trade_mode}")
    print(f"  ✅ Account info OK")
    return True


def find_gold_symbol(mt5) -> str:
    """Detect account-specific symbol name for Gold (e.g. XAUUSDm on Exness Standard)."""
    for candidate in ["XAUUSDm", "XAUUSD", "GOLD"]:
        mt5.symbol_select(candidate, True)
        tick = mt5.symbol_info_tick(candidate)
        if tick is not None:
            return candidate
    return "XAUUSD"


def test_symbol_info(mt5, symbol: str = "XAUUSD"):
    """Get Gold symbol info and tick."""
    print_test(4, f"mt5.symbol_info_tick('{symbol}')")

    # Ensure symbol is visible
    selected = mt5.symbol_select(symbol, True)
    if not selected:
        print(f"  ⚠️ Could not select {symbol}: {mt5.last_error()}")

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print(f"  ❌ Failed: {mt5.last_error()}")
        print(f"  💡 Try adding {symbol} to Market Watch in MT5")
        return False

    print(f"  Symbol:  {symbol}")
    print(f"  Bid:     {tick.bid}")
    print(f"  Ask:     {tick.ask}")
    print(f"  Spread:  {tick.ask - tick.bid:.3f}")
    print(f"  Time:    {datetime.fromtimestamp(tick.time)}")

    # Symbol details
    sym_info = mt5.symbol_info(symbol)
    if sym_info:
        print(f"  Digits:  {sym_info.digits}")
        print(f"  Point:   {sym_info.point}")
        print(f"  Min lot: {sym_info.volume_min}")
        print(f"  Max lot: {sym_info.volume_max}")
        print(f"  Lot step:{sym_info.volume_step}")

    print(f"  ✅ Symbol info OK")
    return True


def test_positions(mt5):
    """Get current open positions."""
    print_test(5, "mt5.positions_get()")

    positions = mt5.positions_get()
    if positions is None:
        error = mt5.last_error()
        if error[0] == -2:
            print("  ℹ️ No open positions (this is normal for a fresh demo)")
        else:
            print(f"  ❌ Failed: {error}")
            return False
    else:
        print(f"  Open positions: {len(positions)}")
        for pos in positions:
            print(f"    Ticket: {pos.ticket} | {pos.symbol} | "
                  f"{'BUY' if pos.type == 0 else 'SELL'} | "
                  f"Vol: {pos.volume} | P/L: {pos.profit}")

    print(f"  ✅ Positions query OK")
    return True


def test_demo_order(mt5, symbol: str = "XAUUSD"):
    """Test opening and closing a demo order."""
    print_test(6, f"Demo Order (BUY 0.01 {symbol})")

    print("\n  ⚠️  This will open a REAL order on your DEMO account.")
    confirm = input("  Execute demo order? (y/n): ").strip().lower()
    if confirm != "y":
        print("  ⏭️ Skipped")
        return True

    # Get current price
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print(f"  ❌ Cannot get tick: {mt5.last_error()}")
        return False

    sym_info = mt5.symbol_info(symbol)
    digits = sym_info.digits if sym_info else 3

    # Safe SL/TP distances for Gold: $5.0 SL, $10.0 TP
    sl_price = round(tick.ask - 5.0, digits)
    tp_price = round(tick.ask + 10.0, digits)

    # Open BUY order
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": 0.01,
        "type": mt5.ORDER_TYPE_BUY,
        "price": tick.ask,
        "sl": sl_price,
        "tp": tp_price,
        "deviation": 20,
        "magic": 234000,
        "comment": "trading-bot-phase0-test",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    print(f"\n  Sending BUY order:")
    print(f"    Price:  {request['price']}")
    print(f"    SL:     {request['sl']}")
    print(f"    TP:     {request['tp']}")
    print(f"    Volume: {request['volume']}")

    start = time.time()
    result = mt5.order_send(request)
    elapsed = (time.time() - start) * 1000

    if result is None:
        print(f"  ❌ order_send returned None: {mt5.last_error()}")
        return False

    print(f"\n  Result:")
    print(f"    Retcode:  {result.retcode}")
    print(f"    Comment:  {result.comment}")
    print(f"    Order:    {result.order}")
    print(f"    Latency:  {elapsed:.0f}ms")

    trade_done = getattr(mt5, "TRADE_RETCODE_DONE", 10009)
    if result.retcode != trade_done:
        print(f"  ❌ Order failed!")
        print(f"  💡 Retcode {result.retcode}: {result.comment}")
        if result.retcode == 10027:
            print("  👉 Please click the 'Algo Trading' button on the top toolbar of MetaTrader 5 (make sure it's GREEN).")
        return False

    print(f"  ✅ BUY order opened! Ticket: {result.order}")
    ticket = result.order

    # Test modify SL/TP ($3.0 SL, $15.0 TP)
    print(f"\n  Modifying SL/TP on ticket {ticket}...")
    modify_request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol": symbol,
        "sl": round(tick.ask - 3.0, digits),
        "tp": round(tick.ask + 15.0, digits),
    }

    start = time.time()
    mod_result = mt5.order_send(modify_request)
    elapsed = (time.time() - start) * 1000

    if mod_result and mod_result.retcode == trade_done:
        print(f"  ✅ SL/TP modified ({elapsed:.0f}ms)")
    else:
        comment = mod_result.comment if mod_result else mt5.last_error()
        print(f"  ⚠️ Modify failed: {comment}")

    # Close order
    print(f"\n  Closing position {ticket}...")
    tick = mt5.symbol_info_tick(symbol)
    close_request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": ticket,
        "symbol": symbol,
        "volume": 0.01,
        "type": mt5.ORDER_TYPE_SELL,
        "price": tick.bid,
        "deviation": 20,
        "magic": 234000,
        "comment": "trading-bot-phase0-close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    start = time.time()
    close_result = mt5.order_send(close_request)
    elapsed = (time.time() - start) * 1000

    if close_result and close_result.retcode == trade_done:
        print(f"  ✅ Position closed ({elapsed:.0f}ms)")
    else:
        comment = close_result.comment if close_result else mt5.last_error()
        print(f"  ⚠️ Close failed: {comment}")
        print(f"  💡 You may need to close ticket {ticket} manually in MT5")

    return True


def test_latency_benchmark(mt5, symbol: str = "XAUUSD"):
    """Benchmark round-trip latency."""
    print_test(7, f"Latency Benchmark ({symbol} - 10 iterations)")

    latencies = []
    for i in range(10):
        start = time.time()
        mt5.symbol_info_tick(symbol)
        elapsed = (time.time() - start) * 1000
        latencies.append(elapsed)

    avg = sum(latencies) / len(latencies)
    min_l = min(latencies)
    max_l = max(latencies)

    print(f"  Average: {avg:.1f}ms")
    print(f"  Min:     {min_l:.1f}ms")
    print(f"  Max:     {max_l:.1f}ms")

    if avg < 100:
        print(f"  ✅ Excellent latency")
    elif avg < 500:
        print(f"  ✅ Acceptable latency")
    else:
        print(f"  ⚠️ High latency — may need optimization")

    return True


def main():
    print_header("Trading Bot — Phase 0: MT5 Bridge Verification")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Python: {sys.version}")

    results = {}

    # Test 0: Import
    mt5 = test_import()
    results["import"] = mt5 is not None
    if mt5 is None:
        print_header("PHASE 0 FAILED — Cannot import MetaTrader5")
        sys.exit(1)

    # Test 1: Initialize
    env = load_env()
    results["initialize"] = test_initialize(mt5, env)
    if not results["initialize"]:
        print_header("PHASE 0 FAILED — Cannot connect to MT5")
        mt5.shutdown()
        sys.exit(1)

    # Detect gold symbol (e.g. XAUUSDm on Exness Standard)
    gold_symbol = find_gold_symbol(mt5)
    print(f"\n  🎯 Detected Gold symbol: {gold_symbol}")

    # Test 2-5: Read operations
    results["terminal_info"] = test_terminal_info(mt5)
    results["account_info"] = test_account_info(mt5)
    results["symbol_info"] = test_symbol_info(mt5, gold_symbol)
    results["positions"] = test_positions(mt5)

    # Test 6: Write operations (interactive)
    results["demo_order"] = test_demo_order(mt5, gold_symbol)

    # Test 7: Latency
    results["latency"] = test_latency_benchmark(mt5, gold_symbol)

    # Cleanup
    mt5.shutdown()

    # Summary
    print_header("PHASE 0 RESULTS")
    all_passed = True
    for test_name, passed in results.items():
        icon = "✅" if passed else "❌"
        print(f"  {icon} {test_name}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("  🎉 ALL TESTS PASSED — Python ↔ MT5 ↔ Exness bridge is working!")
        print("  ➡️  Proceed to Phase 1: Project Foundation")
    else:
        print("  ⚠️  Some tests failed. Review errors above.")
        print("  💡 Fix issues before proceeding to Phase 1.")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
