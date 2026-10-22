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
LOG_FILE="$PROJECT_DIR/logs/bot_error.log"
ERR_FILE="$PROJECT_DIR/logs/bot_error.log"

# Colors for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

ensure_installed() {
    # Luôn đồng bộ cấu hình LaunchAgent với thư mục dự án hiện tại
    install_service
}

install_service() {
    mkdir -p "$HOME/Library/LaunchAgents"
    mkdir -p "$PROJECT_DIR/logs"
    chmod +x "$PROJECT_DIR/scripts/run_daemon.sh" 2>/dev/null || true
    chmod +x "$PROJECT_DIR/scripts/bot.sh" 2>/dev/null || true

    # Dynamically inject the actual PROJECT_DIR into the plist file
    sed "s|/Users/itic/Documents/trading-bot|$PROJECT_DIR|g" "$PLIST_SRC" > "$PLIST_DEST"
    echo -e "${GREEN}✅ Đã cấu hình LaunchAgent tại: $PLIST_DEST${NC}"
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

    # Stop any manual or background running run.py instance first
    pkill -f "caffeinate.*run.py" 2>/dev/null
    pkill -f "python.*run.py" 2>/dev/null
    sleep 1

    # 1. Thử khởi động bằng launchctl (macOS LaunchAgent)
    launchctl bootout "gui/$(id -u)/$SERVICE_NAME" 2>/dev/null
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST" 2>/dev/null || launchctl load -w "$PLIST_DEST" 2>/dev/null
    launchctl kickstart -k "gui/$(id -u)/$SERVICE_NAME" 2>/dev/null || launchctl start "$SERVICE_NAME" 2>/dev/null

    sleep 2

    # 2. Nếu launchctl không chạy được (ví dụ kết nối qua SSH không có Aqua session), tự động chạy nền bằng nohup
    if ! pgrep -f "python.*run.py" >/dev/null; then
        echo -e "${YELLOW}ℹ️  LaunchAgent không phản hồi (thường do môi trường SSH), đang khởi động daemon nền (nohup)...${NC}"
        nohup "$PROJECT_DIR/scripts/run_daemon.sh" >/dev/null 2>> "$ERR_FILE" &
        sleep 2
    fi

    echo -e "${GREEN}🚀 Đã kích hoạt Trading Bot chạy nền!${NC}"
    status_service
}

stop_service() {
    echo -e "${YELLOW}⏹️  Đang dừng Trading Bot...${NC}"
    launchctl bootout "gui/$(id -u)/$SERVICE_NAME" 2>/dev/null
    launchctl unload "$PLIST_DEST" 2>/dev/null
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
        if [ -s "$ERR_FILE" ]; then
            echo ""
            echo -e "${RED}⚠️  Log lỗi gần nhất ($ERR_FILE):${NC}"
            tail -n 12 "$ERR_FILE"
        fi
        if [ "$LOG_FILE" != "$ERR_FILE" ] && [ -s "$LOG_FILE" ]; then
            echo ""
            echo -e "${YELLOW}📜 Dòng log cuối ($LOG_FILE):${NC}"
            tail -n 8 "$LOG_FILE"
        fi
    fi

    echo ""
    echo "LaunchAgent : $([ -f "$PLIST_DEST" ] && echo -e "${GREEN}Đã cài đặt${NC}" || echo -e "${RED}Chưa cài đặt${NC}")"
    echo "Log file    : $LOG_FILE"
    echo "Error file  : $ERR_FILE"
    echo "=================================================="
    echo -e "📌 Lệnh hữu ích:"
    echo -e "  - Xem log hoạt động : ${GREEN}./scripts/bot.sh logs${NC}"
    echo -e "  - Xem log lỗi       : ${YELLOW}./scripts/bot.sh errors${NC}"
    echo -e "  - Kiểm tra trạng thái: ${BLUE}./scripts/bot.sh status${NC}"
    echo -e "  - Chạy trực tiếp    : ${GREEN}./scripts/bot.sh run${NC} (hoặc python run.py)"
    echo "=================================================="
}

view_logs() {
    mkdir -p "$PROJECT_DIR/logs"
    touch "$LOG_FILE"
    echo -e "${BLUE}📜 Đang xem live log ($LOG_FILE)... Nhấn Ctrl+C để thoát.${NC}"
    tail -F -n 50 "$LOG_FILE"
}

view_errors() {
    mkdir -p "$PROJECT_DIR/logs"
    touch "$ERR_FILE"
    echo -e "${RED}⚠️  Đang xem error log ($ERR_FILE)... Nhấn Ctrl+C để thoát.${NC}"
    tail -F -n 50 "$ERR_FILE"
}

run_foreground() {
    echo -e "${BLUE}🚀 Đang chạy Trading Bot trực tiếp trên Terminal...${NC}"
    pkill -f "caffeinate.*run.py" 2>/dev/null
    pkill -f "python.*run.py" 2>/dev/null
    sleep 1
    "$PROJECT_DIR/scripts/run_daemon.sh"
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
    run)
        run_foreground
        ;;
    *)
        echo "Sử dụng: $0 {start|stop|restart|status|logs|errors|run|install|uninstall}"
        exit 1
        ;;
esac
