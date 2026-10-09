# Phase 0 — MT5 Bridge Verification Results

> **Date**: 2026-10-01  
> **Platform**: Mac mini Apple Silicon M4 • macOS Darwin 24.6.0  
> **Python**: 3.12.3  
> **Broker**: Exness (Server: `Exness-MT5Trial14`)  
> **Account**: Demo Standard (`416446102`)  
> **Terminal**: MetaTrader 5 Build 6231 (x64 Wine 11.1 bundle)

---

## 1. Executive Summary

| Test | Status | Metric / Detail |
|------|--------|-----------------|
| Import package | ✅ PASSED | `mt5_mac` Apple Silicon bridge v0.3.0 |
| Terminal Initialize | ✅ PASSED | 1,770ms via Wine IPC `/portable` |
| Terminal Info | ✅ PASSED | MetaQuotes Ltd., Build 6231, Trade OK: True |
| Account Info | ✅ PASSED | Exness-MT5Trial14, Balance: 500.00 USD, Leverage: 1:2B |
| Symbol Info (Gold) | ✅ PASSED | Auto-detected `XAUUSDm` (Bid: 4,153.119, Spread: 0.240) |
| Positions Query | ✅ PASSED | 0 open positions queried cleanly |
| Open Market Order | ✅ PASSED | BUY 0.01 `XAUUSDm` (Ticket: `3918303843`, Latency: 98ms) |
| Close Position | ✅ PASSED | Fully closed position `3918303843` (Latency: 71ms) |
| Latency Benchmark | ✅ PASSED | **0.5ms average round-trip** (Min: 0.3ms, Max: 0.7ms) |

---

## 2. Technical Findings & Solutions Applied

### 2.1 PyPI Architecture Incompatibility
* **Issue**: Official `MetaTrader5` PyPI package only distributes Windows AMD64/x86 wheels (`.whl`), with no native macOS binary.
* **Solution**: Integrated `mt5_mac` which uses the Wine runtime already embedded in official `MetaTrader 5.app` to host the Windows Python + `MetaTrader5` package.

### 2.2 Wine Binary Path in macOS App Bundle
* **Issue**: `mt5_mac` initially looked for `wine64`, whereas MetaQuotes' macOS package uses `wine`.
* **Fix**: Patched `bootstrap.py` and `backend.py` to auto-detect both `wine` and `wine64`.

### 2.3 Python 3.9 Embeddable Bootstrap Pip
* **Issue**: `bootstrap.pypa.io/get-pip.py` dropped support for Python 3.9.
* **Fix**: Updated bootstrap source to `https://bootstrap.pypa.io/pip/3.9/get-pip.py`.

### 2.4 Exness Account Symbol Suffix
* **Issue**: `XAUUSD` is suffixed with `m` on Exness Standard accounts (`XAUUSDm`).
* **Fix**: Added dynamic gold symbol detection (`find_gold_symbol`) and configured `TRADING_ALLOWED_SYMBOLS=XAUUSD,XAUUSDm` in `.env`.

### 2.5 Stop Loss Distance
* **Issue**: Error 10016 (`Invalid stops`) when SL was placed within the broker spread.
* **Fix**: Set safe SL/TP distances relative to digits precision ($5.0 SL, $10.0 TP on Gold).

---

## 3. Verdict

**Phase 0 is 100% COMPLETE & VERIFIED.**  
The local macOS Apple Silicon bridge communicates flawlessly with MT5 & Exness with sub-millisecond query latency and sub-100ms order execution times.  
The project is approved to proceed into **Phase 3 (Signal Listener)** and **Phase 4 (Signal Parser)**.
