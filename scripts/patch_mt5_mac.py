"""
Auto-patcher for mt5_mac on Apple Silicon (M1/M2/M3/M4).

Ensures:
1. Wine binary path compatibility (wine vs wine64).
2. Python 3.9 pip bootstrap URL.
3. python39._pth includes Lib\\site-packages.
4. terminal_info() API support.
5. MetaTrader5 package verification & installation inside Wine Python.

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

        # Ensure python39._pth includes Lib\site-packages in _install_python_embeddable
        old_pth_code = 'content = content.replace("#import site", "import site")'
        new_pth_code = (
            'content = content.replace("#import site", "import site").replace("# import site", "import site")\n'
            '        if "Lib\\\\site-packages" not in content and "Lib/site-packages" not in content:\n'
            '            content = "python39.zip\\n.\\nLib\\\\site-packages\\nimport site\\n"'
        )
        if old_pth_code in content:
            content = content.replace(old_pth_code, new_pth_code)

        # Ensure python39._pth is auto-fixed in ensure_environment before installing MT5
        check_mt5_target = '    if not _check_mt5_installed(wine_bin, wineprefix, python_exe_wine):'
        if check_mt5_target in content and "_fix_pth" not in content:
            pth_fix_snippet = (
                '    # Auto-fix python39._pth so pip and site-packages can be imported\n'
                '    _pth_file = os.path.join(wineprefix, "drive_c", "Python39", "python39._pth")\n'
                '    if os.path.exists(_pth_file):\n'
                '        try:\n'
                '            with open(_pth_file, "r") as _fix_pth:\n'
                '                _pth_data = _fix_pth.read()\n'
                '            if "Lib\\\\site-packages" not in _pth_data and "Lib/site-packages" not in _pth_data:\n'
                '                with open(_pth_file, "w") as _fix_pth:\n'
                '                    _fix_pth.write("python39.zip\\n.\\nLib\\\\site-packages\\nimport site\\n")\n'
                '        except Exception:\n'
                '            pass\n\n'
                '    if not _check_mt5_installed(wine_bin, wineprefix, python_exe_wine):'
            )
            content = content.replace(check_mt5_target, pth_fix_snippet, 1)

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

    # 6. Fix python39._pth in Wine Python directory if already extracted
    wineprefix = os.environ.get(
        "WINEPREFIX",
        os.path.expanduser("~/Library/Application Support/net.metaquotes.wine.metatrader5")
    )
    wine_pth = os.path.join(wineprefix, "drive_c", "Python39", "python39._pth")
    try:
        if os.path.exists(wine_pth):
            with open(wine_pth, "r", encoding="utf-8") as f:
                pth_content = f.read()
            if "Lib\\site-packages" not in pth_content and "Lib/site-packages" not in pth_content:
                with open(wine_pth, "w", encoding="utf-8") as f:
                    f.write("python39.zip\n.\nLib\\site-packages\nimport site\n")
                print("  ✅ Fixed python39._pth in Wine Python directory (enabled Lib\\site-packages).")
            else:
                print("  ✅ python39._pth already contains Lib\\site-packages.")
    except Exception as e:
        print(f"  ⚠️ Could not update wine python39._pth directly: {e}")

    # 7. Check and install MetaTrader5 inside Wine Python if needed
    try:
        import subprocess
        wine_bin = "/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"
        wine_bin64 = "/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine64"
        actual_wine = wine_bin64 if os.path.exists(wine_bin64) else wine_bin
        python_exe = os.path.join(wineprefix, "drive_c", "Python39", "python.exe")

        if os.path.exists(actual_wine) and os.path.exists(python_exe):
            env = {**os.environ, "WINEPREFIX": wineprefix}
            check = subprocess.run(
                [actual_wine, "C:\\Python39\\python.exe", "-c", "import MetaTrader5; print('ok')"],
                capture_output=True, text=True, env=env
            )
            if check.returncode != 0 or check.stdout.strip() != "ok":
                print("  📦 Installing MetaTrader5 package inside Wine Python...")
                install_res = subprocess.run(
                    [actual_wine, "C:\\Python39\\python.exe", "-m", "pip", "install", "MetaTrader5"],
                    capture_output=True, text=True, env=env, timeout=180
                )
                if install_res.returncode == 0:
                    print("  ✅ MetaTrader5 package installed successfully in Wine!")
                else:
                    print(f"  ⚠️ Pip install MetaTrader5 returned code {install_res.returncode}")
            else:
                print("  ✅ MetaTrader5 package is verified inside Wine.")
    except Exception as e:
        print(f"  ⚠️ Could not verify Wine MetaTrader5: {e}")

    print("\n🎉 mt5_mac is fully patched and ready on Apple Silicon!")


if __name__ == "__main__":
    patch_mt5_mac()
