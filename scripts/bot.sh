#!/usr/bin/env bash
# ==============================================================================
# Trading Bot Management CLI for macOS
# Usage:
#   ./scripts/bot.sh start     # Start daemon (auto-restarting, survives terminal close)
#   ./scripts/bot.sh stop      # Stop daemon
#   ./scripts/bot.sh restart   # Restart daemon
#   ./scripts/bot.sh status    # Check daemon health & running status
#   ./scripts/bot.sh logs      # Tail live application logs
#   ./scripts/bot.sh errors    # Tail error logs
#   ./scripts/bot.sh install   # Register as macOS LaunchAgent (start on boot)
#   ./scripts/bot.sh uninstall # Remove from macOS LaunchAgents
# ==============================================================================

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="com.tradingbot"
PLIST_SRC="$PROJECT_DIR/scripts/com.tradingbot.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$SERVICE_NAME.plist"
LOG_FILE="$PROJECT_DIR/logs/bot.log"
ERR_FILE="$PROJECT_DIR/logs/bot_error.log"

# Colors for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

ensure_installed() {
    if [ ! -f "$PLIST_DEST" ]; then
        echo -e "${YELLOW}ℹ️  LaunchAgent chưa được cài đặt. Đang tiến hành cài đặt...${NC}"
        install_service
    fi
}

install_service() {
    mkdir -p "$HOME/Library/LaunchAgents"
    mkdir -p "$PROJECT_DIR/logs"
    chmod +x "$PROJECT_DIR/scripts/run_daemon.sh"
    chmod +x "$PROJECT_DIR/scripts/bot.sh"

    # Dynamically inject the actual PROJECT_DIR into the plist file
    sed "s|/Users/itic/Documents/trading-bot|$PROJECT_DIR|g" "$PLIST_SRC" > "$PLIST_DEST"
    echo -e "${GREEN}✅ Đã cài đặt LaunchAgent tại: $PLIST_DEST${NC}"
}

uninstall_service() {
    stop_service
    if [ -f "$PLIST_DEST" ]; then
        rm -f "$PLIST_DEST"
        echo -e "${GREEN}✅ Đã gỡ bỏ LaunchAgent khỏi hệ thống.${NC}"
    else
        echo -e "${YELLOW}ℹ️  LaunchAgent không tồn tại.${NC}"
    fi
}

start_service() {
    ensure_installed
    mkdir -p "$PROJECT_DIR/logs"

    # Stop any manual running run.py instance first to avoid conflict
    pkill -f "python.*run.py" 2>/dev/null

    # Load and start via launchctl
    launchctl bootout "gui/$(id -u)/$SERVICE_NAME" 2>/dev/null
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST" 2>/dev/null || launchctl load -w "$PLIST_DEST" 2>/dev/null

    sleep 1
    echo -e "${GREEN}🚀 Đã kích hoạt Trading Bot chạy nền trên macOS!${NC}"
    status_service
}

stop_service() {
    echo -e "${YELLOW}⏹️  Đang dừng Trading Bot...${NC}"
    launchctl bootout "gui/$(id -u)/$SERVICE_NAME" 2>/dev/null || launchctl unload "$PLIST_DEST" 2>/dev/null
    pkill -f "caffeinate.*run.py" 2>/dev/null
    pkill -f "python.*run.py" 2>/dev/null
    sleep 1
    echo -e "${GREEN}✅ Bot đã dừng thành công.${NC}"
}

restart_service() {
    echo -e "${BLUE}🔄 Đang khởi động lại Trading Bot...${NC}"
    stop_service
    sleep 2
    start_service
}

status_service() {
    echo "=================================================="
    echo " 🤖 TRADING BOT — macOS DAEMON STATUS"
    echo "=================================================="

    PIDS=$(pgrep -f "python.*run.py")
    if [ -n "$PIDS" ]; then
        echo -e "Trạng thái: ${GREEN}🟢 ĐANG CHẠY (ACTIVE)${NC}"
        echo "PID(s)    : $PIDS"
        ps -o pid,user,%cpu,%mem,etime,command -p $PIDS 2>/dev/null || true
        echo ""
        echo "Tính năng giữ máy thức (caffeinate):"
        if pgrep -f "caffeinate.*run.py" >/dev/null; then
            echo -e "  ${GREEN}✅ Đang bật (Ngăn Mac tự động Sleep khi bot chạy)${NC}"
        else
            echo -e "  ${YELLOW}⚠️  Không hoạt động${NC}"
        fi
    else
        echo -e "Trạng thái: ${RED}🔴 ĐANG DỪNG (STOPPED)${NC}"
    fi

    echo ""
    echo "LaunchAgent : $([ -f "$PLIST_DEST" ] && echo -e "${GREEN}Đã cài đặt${NC}" || echo -e "${RED}Chưa cài đặt${NC}")"
    echo "Log file    : $LOG_FILE"
    echo "Error file  : $ERR_FILE"
    echo "=================================================="
}

view_logs() {
    mkdir -p "$PROJECT_DIR/logs"
    touch "$LOG_FILE"
    echo -e "${BLUE}📜 Đang xem live log ($LOG_FILE)... Nhấn Ctrl+C để thoát.${NC}"
    tail -f -n 50 "$LOG_FILE"
}

view_errors() {
    mkdir -p "$PROJECT_DIR/logs"
    touch "$ERR_FILE"
    echo -e "${RED}⚠️  Đang xem error log ($ERR_FILE)... Nhấn Ctrl+C để thoát.${NC}"
    tail -f -n 50 "$ERR_FILE"
}

case "$1" in
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        restart_service
        ;;
    status)
        status_service
        ;;
    logs)
        view_logs
        ;;
    errors)
        view_errors
        ;;
    install)
        install_service
        ;;
    uninstall)
        uninstall_service
        ;;
    *)
        echo "Sử dụng: $0 {start|stop|restart|status|logs|errors|install|uninstall}"
        exit 1
        ;;
esac
