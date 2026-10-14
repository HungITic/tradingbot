"""
Auto-patcher for mt5_mac on Apple Silicon (M1/M2/M3/M4).

Ensures:
1. Wine binary path compatibility (wine vs wine64).
2. Python 3.9 pip bootstrap URL.
3. terminal_info() API support.

Run:
    python scripts/patch_mt5_mac.py
"""

import importlib.util
import os
import sys


def patch_mt5_mac() -> None:
    spec = importlib.util.find_spec("mt5_mac")
    if not spec or not spec.origin:
        print("❌ mt5_mac is not installed in the current Python environment.")
        print("   Please run: pip install mt5_mac")
        sys.exit(1)

    pkg_dir = os.path.dirname(spec.origin)
    print(f"🔧 Found mt5_mac at: {pkg_dir}")

    # 1. Patch bootstrap.py
    bootstrap_file = os.path.join(pkg_dir, "bootstrap.py")
    if os.path.exists(bootstrap_file):
        with open(bootstrap_file, "r", encoding="utf-8") as f:
            content = f.read()

        # Update get-pip URL for python 3.9
        old_url = 'GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"'
        new_url = 'GET_PIP_URL = "https://bootstrap.pypa.io/pip/3.9/get-pip.py"'
        if old_url in content:
            content = content.replace(old_url, new_url)

        # Update wine binary fallback
        if 'WINE_BIN = f"{MT5_APP}/Contents/SharedSupport/wine/bin/wine64"' in content:
            content = content.replace(
                'WINE_BIN = f"{MT5_APP}/Contents/SharedSupport/wine/bin/wine64"',
                '_wine64 = f"{MT5_APP}/Contents/SharedSupport/wine/bin/wine64"\n'
                '_wine = f"{MT5_APP}/Contents/SharedSupport/wine/bin/wine"\n'
                'WINE_BIN = _wine64 if os.path.exists(_wine64) else _wine',
            )

        with open(bootstrap_file, "w", encoding="utf-8") as f:
            f.write(content)
        print("  ✅ bootstrap.py checked/patched.")

    # 2. Patch backend.py
    backend_file = os.path.join(pkg_dir, "backend.py")
    if os.path.exists(backend_file):
        with open(backend_file, "r", encoding="utf-8") as f:
            content = f.read()

        # Update wine binary fallback
        if 'WINE_BIN = f"{MT5_APP}/Contents/SharedSupport/wine/bin/wine64"' in content:
            content = content.replace(
                'WINE_BIN = f"{MT5_APP}/Contents/SharedSupport/wine/bin/wine64"',
                '_wine64 = f"{MT5_APP}/Contents/SharedSupport/wine/bin/wine64"\n'
                '_wine = f"{MT5_APP}/Contents/SharedSupport/wine/bin/wine"\n'
                'WINE_BIN = _wine64 if os.path.exists(_wine64) else _wine',
            )

        # Fix any mangled indentation of account_info from prior patch
        import re
        content = re.sub(r"\n(?<! )def account_info\(", r"\n    def account_info(", content)

        # Ensure terminal_info method exists in MT5Backend
        if "def terminal_info(" not in content:
            target = "    def account_info("
            method_code = (
                "    def terminal_info(self) -> MqlTerminalInfo | None:\n"
                '        result = self._send({"action": "terminal_info"})\n'
                "        if result:\n"
                "            fields = {k: result.get(k) for k in MqlTerminalInfo._fields if k in result}\n"
                "            return MqlTerminalInfo(**fields)\n"
                "        return None\n\n"
                "    def account_info("
            )
            if target in content:
                content = content.replace(target, method_code, 1)
            if "MqlTerminalInfo" not in content[:content.find("class MT5Backend")]:
                content = content.replace(
                    "from mt5_mac.types import (",
                    "from mt5_mac.types import (\n    MqlTerminalInfo,",
                )

        with open(backend_file, "w", encoding="utf-8") as f:
            f.write(content)
        print("  ✅ backend.py checked/patched.")

    # 3. Patch types.py
    types_file = os.path.join(pkg_dir, "types.py")
    if os.path.exists(types_file):
        with open(types_file, "r", encoding="utf-8") as f:
            content = f.read()

        if "class MqlTerminalInfo" not in content:
            terminal_info_cls = '''
class MqlTerminalInfo(NamedTuple):
    community_account: bool
    community_connection: bool
    connected: bool
    dlls_allowed: bool
    trade_allowed: bool
    tradeapi_disabled: bool
    email_enabled: bool
    ftp_enabled: bool
    notifications_enabled: bool
    mqid: bool
    build: int
    maxbars: int
    codepage: int
    ping_last: int
    community_balance: float
    retransmission: float
    company: str
    name: str
    language: str
    path: str
    data_path: str
    commondata_path: str
'''
            content += "\n" + terminal_info_cls
            with open(types_file, "w", encoding="utf-8") as f:
                f.write(content)
        print("  ✅ types.py checked/patched.")

    # 4. Patch agent.py
    agent_file = os.path.join(pkg_dir, "agent.py")
    if os.path.exists(agent_file):
        with open(agent_file, "r", encoding="utf-8") as f:
            content = f.read()

        import re
        content = re.sub(r'\n(?<! )if action == "account_info":', r'\n    if action == "account_info":', content)

        if 'action == "terminal_info"' not in content:
            target = '    if action == "account_info":'
            snippet = (
                '    if action == "terminal_info":\n'
                '        info = mt5.terminal_info()\n'
                '        return _serialize(info) if info else {}\n\n'
                '    if action == "account_info":'
            )
            if target in content:
                content = content.replace(target, snippet, 1)
                with open(agent_file, "w", encoding="utf-8") as f:
                    f.write(content)
        print("  ✅ agent.py checked/patched.")

    # 5. Patch __init__.py
    init_file = os.path.join(pkg_dir, "__init__.py")
    if os.path.exists(init_file):
        with open(init_file, "r", encoding="utf-8") as f:
            content = f.read()

        if "def terminal_info(" not in content:
            export_code = '''
def terminal_info() -> MqlTerminalInfo | None:
    return _backend.terminal_info()
'''
            content += "\n" + export_code
            if "MqlTerminalInfo" not in content[:content.find("_backend: MT5Backend")]:
                content = content.replace(
                    "from mt5_mac.types import (",
                    "from mt5_mac.types import (\n    MqlTerminalInfo,",
                )
            with open(init_file, "w", encoding="utf-8") as f:
                f.write(content)
        print("  ✅ __init__.py checked/patched.")

    print("\n🎉 mt5_mac is fully patched and ready on Apple Silicon!")


if __name__ == "__main__":
    patch_mt5_mac()
