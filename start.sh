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

# 2. Очистка и создание окружения
echo "--- RECREATING VIRTUAL ENVIRONMENT ---" | tee -a $LOG_FILE
rm -rf venv
python3 -m venv venv
source venv/bin/activate

# 3. Обновление базовых инструментов
pip install --upgrade pip --quiet
echo "--- INSTALLING DEPENDENCIES ---" | tee -a $LOG_FILE
pip install --no-cache-dir -r requirements.txt --quiet

# 4. Финальная проверка перед стартом
echo "--- ENVIRONMENT READY. LAUNCHING BOT ---" | tee -a $LOG_FILE

# 5. Цикл для авто-перезагрузки (если бот упадет — он поднимется снова)
while true; do
    echo "[$(date)] Bot is running..." | tee -a $LOG_FILE
    python3 main.py >> bot_runtime.log 2>&1
    
    echo "[WARNING] Bot crashed. Restarting in 5 seconds..." | tee -a $LOG_FILE
    sleep 5
done