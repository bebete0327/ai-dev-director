#!/usr/bin/env bash
# 24/7 Linux Cloud Background Service Manager for AI-Dev-Director
# Usage:
#   bash scripts/cloud_service.sh install   # Install deps, CLIs, and start 24/7 background service
#   bash scripts/cloud_service.sh start     # Start background server (0.0.0.0:8081)
#   bash scripts/cloud_service.sh stop      # Stop background server
#   bash scripts/cloud_service.sh status    # Show status & PC/Mobile connection URLs
#   bash scripts/cloud_service.sh logs      # Tail live background logs
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
PID_FILE="${ROOT_DIR}/data/cloud_server.pid"
LOG_FILE="${ROOT_DIR}/data/cloud_server.log"
PORT="${DIRECTOR_PORT:-8081}"
SERVICE_NAME="ai-dev-director"

mkdir -p "${ROOT_DIR}/data"

ensure_venv() {
    if [ ! -x "${VENV_DIR}/bin/python" ]; then
        echo "[1/3] Python 가상환경(.venv) 생성 중..."
        python3 -m venv "${VENV_DIR}"
    fi
    echo "[2/3] Python 필수 패키지 설치 중..."
    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    "${VENV_DIR}/bin/pip" install --quiet -r "${ROOT_DIR}/requirements.txt"
}

install_clis_if_available() {
    if command -v npm >/dev/null 2>&1; then
        echo "[3/3] Claude Code / OpenAI Codex CLI 확인 중..."
        command -v claude >/dev/null 2>&1 || npm install -g @anthropic-ai/claude-code --prefix "${HOME}/.local" || true
        command -v codex  >/dev/null 2>&1 || npm install -g @openai/codex --prefix "${HOME}/.local" || true
    fi
}

print_urls() {
    local lan_ip=""
    local ts_ip=""
    local pub_ip=""
    lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    if command -v tailscale >/dev/null 2>&1; then
        ts_ip="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
    fi
    pub_ip="$(curl -s --max-time 2 https://ifconfig.me || true)"

    echo "================================================================"
    echo " AI DEV DIRECTOR 24/7 클라우드 백그라운드 서버 실행 중 (Port ${PORT})"
    echo "================================================================"
    echo " - 로컬 접속:         http://127.0.0.1:${PORT}"
    [ -n "${lan_ip}" ] && echo " - 내부망/Wi-Fi 접속: http://${lan_ip}:${PORT}"
    [ -n "${ts_ip}" ]  && echo " - Tailscale(추천):   http://${ts_ip}:${PORT}  (스마트폰/PC 어디서나 안전 접속)"
    [ -n "${pub_ip}" ] && echo " - 공인 IP 접속:      http://${pub_ip}:${PORT}  (클라우드 방화벽 ${PORT} 포트 개방 시)"
    echo "================================================================"
}

start_daemon() {
    if [ -f "${PID_FILE}" ] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
        echo "이미 백그라운드에서 실행 중입니다 (PID $(cat "${PID_FILE}"))."
        print_urls
        return 0
    fi
    export DIRECTOR_REMOTE_ACCESS=1
    export DIRECTOR_BIND_HOST=0.0.0.0
    export DIRECTOR_PORT="${PORT}"
    export PATH="${HOME}/.local/bin:${PATH}"

    nohup "${VENV_DIR}/bin/python" "${ROOT_DIR}/launcher.py" --cloud >> "${LOG_FILE}" 2>&1 &
    local pid=$!
    echo "${pid}" > "${PID_FILE}"
    sleep 1
    print_urls
}

stop_daemon() {
    if systemctl --user is-active "${SERVICE_NAME}.service" >/dev/null 2>&1; then
        systemctl --user stop "${SERVICE_NAME}.service" || true
    fi
    if [ -f "${PID_FILE}" ]; then
        local pid
        pid="$(cat "${PID_FILE}")"
        if kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" 2>/dev/null || true
            sleep 1
        fi
        rm -f "${PID_FILE}"
    fi
    echo "AI DEV DIRECTOR 백그라운드 서버가 중지되었습니다."
}

install_systemd_user() {
    if command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
        local unit_dir="${HOME}/.config/systemd/user"
        mkdir -p "${unit_dir}"
        cat > "${unit_dir}/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=AI Dev Director 24/7 Cloud & Mobile Background Server
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${ROOT_DIR}
Environment="DIRECTOR_REMOTE_ACCESS=1"
Environment="DIRECTOR_BIND_HOST=0.0.0.0"
Environment="DIRECTOR_PORT=${PORT}"
Environment="PATH=${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=${VENV_DIR}/bin/python ${ROOT_DIR}/launcher.py --cloud
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable --now "${SERVICE_NAME}.service"
        loginctl enable-linger "${USER}" 2>/dev/null || true
        echo "systemd 백그라운드 서비스(${SERVICE_NAME}.service)가 등록 및 시작되었습니다."
        print_urls
        return 0
    fi
    start_daemon
}

cmd="${1:-status}"
case "${cmd}" in
    install)
        ensure_venv
        install_clis_if_available
        install_systemd_user
        ;;
    start)
        ensure_venv
        start_daemon
        ;;
    stop)
        stop_daemon
        ;;
    restart)
        stop_daemon
        ensure_venv
        start_daemon
        ;;
    status)
        if [ -f "${PID_FILE}" ] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
            echo "상태: 실행 중 (PID $(cat "${PID_FILE}"))"
            print_urls
        elif command -v systemctl >/dev/null 2>&1 && systemctl --user is-active "${SERVICE_NAME}.service" >/dev/null 2>&1; then
            echo "상태: systemd 서비스 실행 중"
            print_urls
        else
            echo "상태: 중지됨"
        fi
        ;;
    logs)
        tail -n 80 -f "${LOG_FILE}"
        ;;
    *)
        echo "사용법: bash scripts/cloud_service.sh {install|start|stop|restart|status|logs}"
        exit 1
        ;;
esac
