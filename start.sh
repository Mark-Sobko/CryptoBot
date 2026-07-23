#!/bin/bash

# Устанавливаем строгий режим: выходим при ошибке любого из этапов
set -e

LOG_FILE="deploy_log.txt"
echo "--- [$(date)] STARTING CLEAN BOT DEPLOYMENT ---" | tee -a $LOG_FILE

# 1. Проверка наличия requirements.txt
if [ ! -f "requirements.txt" ]; then
    echo "[ERROR] requirements.txt не найден! Проверь директорию." | tee -a $LOG_FILE
    exit 1
fi

# 2. Создание окружения без удаления существующего runtime
VENV_DIR="${VENV_DIR:-.venv}"
if [ ! -d "$VENV_DIR" ]; then
    echo "--- CREATING VIRTUAL ENVIRONMENT: $VENV_DIR ---" | tee -a $LOG_FILE
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# 3. Обновление базовых инструментов
pip install --upgrade pip --quiet
echo "--- INSTALLING DEPENDENCIES ---" | tee -a $LOG_FILE
pip install --no-cache-dir -r requirements.txt --quiet

# 4. Финальная проверка перед стартом
echo "--- ENVIRONMENT READY. LAUNCHING BOT ---" | tee -a $LOG_FILE

# 5. Безопасные значения запуска: demo by default, no infinite restart by default
export BYBIT_DEMO="${BYBIT_DEMO:-true}"
export BYBIT_TESTNET="${BYBIT_TESTNET:-false}"

if [ "$BYBIT_DEMO" != "true" ] && [ "$BYBIT_TESTNET" != "true" ] && [ "${ALLOW_LIVE_TRADING:-false}" != "true" ]; then
    echo "[ERROR] Refusing live mode. Set BYBIT_DEMO=true or BYBIT_TESTNET=true." | tee -a $LOG_FILE
    exit 1
fi

MAX_RESTARTS="${MAX_RESTARTS:-0}"
restart_count=0

while true; do
    echo "[$(date)] Bot is running..." | tee -a $LOG_FILE
    set +e
    python3 main.py >> bot_runtime.log 2>&1
    status=$?
    set -e

    if [ "$restart_count" -ge "$MAX_RESTARTS" ]; then
        echo "[INFO] Bot exited with status $status. Restart limit reached ($MAX_RESTARTS)." | tee -a $LOG_FILE
        exit "$status"
    fi

    restart_count=$((restart_count + 1))
    echo "[WARNING] Bot exited with status $status. Restarting in 5 seconds ($restart_count/$MAX_RESTARTS)..." | tee -a $LOG_FILE
    sleep 5
done
