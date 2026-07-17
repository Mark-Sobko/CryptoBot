import sqlite3
import pandas as pd

# Подключаемся к твоей базе
conn = sqlite3.connect('data/bot_memory.db') 

# Запрос, который покажет нам параметры входа (цену, стоп, POI)
# Заменил sl_price на stop_loss (согласно структуре БД из твоего TradeDatabase)
query = """
SELECT symbol, side, entry_price, stop_loss, entry_time, pnl_usd, status
FROM trades 
WHERE symbol IN ('SUIUSDT', 'INJUSDT', 'WIFUSDT', 'TIAUSDT')
ORDER BY entry_time DESC 
LIMIT 20
"""

df = pd.read_sql(query, conn)
print("--- Последние сделки по выбранным монетам ---")
print(df.head(10))

# =========================================================================
# [INSTITUTIONAL SCALING] Аналитика эффективности (Performance Analysis)
# =========================================================================
if not df.empty:
    print("\n--- Институциональная статистика ---")
    
    # 1. Считаем WinRate по этим активам
    closed_trades = df[df['status'] == 'CLOSED']
    if not closed_trades.empty:
        wins = len(closed_trades[closed_trades['pnl_usd'] > 0])
        total = len(closed_trades)
        winrate = (wins / total) * 100
        
        print(f"Кол-во закрытых сделок: {total}")
        print(f"WinRate: {winrate:.2f}%")
        print(f"Суммарный PnL: {closed_trades['pnl_usd'].sum():.2f} USD")
    else:
        print("Нет закрытых сделок для анализа статистики.")

conn.close()