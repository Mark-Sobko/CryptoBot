# Security Cleanup Runbook

Этот репозиторий ранее содержал runtime-секреты и приватные артефакты:

- `.env`
- `data/*.db*`
- `data/*.json`
- `logs/*`
- `*.log`
- `__pycache__/`
- `*.pyc`
- `.DS_Store`
- `*.icloud`

Текущий cleanup-коммит удаляет эти файлы из HEAD, но не удаляет их из уже опубликованной Git-истории.

## 1. Перед очисткой истории

1. Убедиться, что Bybit API keys и Telegram token уже заменены.
2. Предупредить всех, кто работает с репозиторием: после force-push им потребуется свежий clone или hard reset на новую историю.
3. Сохранить нужные приватные runtime-файлы вне Git.

## 2. Вариант через git-filter-repo

Выполнять из свежего клона или mirror-клона:

```bash
git clone --mirror git@github.com:Mark-Sobko/CryptoBot.git CryptoBot.git
cd CryptoBot.git

git filter-repo --force \
  --path .env --invert-paths \
  --path-glob 'data/*.db' --invert-paths \
  --path-glob 'data/*.db-*' --invert-paths \
  --path-glob 'data/*.json' --invert-paths \
  --path-glob 'logs/*' --invert-paths \
  --path-glob '*.log' --invert-paths \
  --path-glob '__pycache__/*' --invert-paths \
  --path-glob '*.pyc' --invert-paths \
  --path-glob '.DS_Store' --invert-paths \
  --path-glob '*.icloud' --invert-paths

git push --force --mirror
```

`git-filter-repo` может потребовать установки:

```bash
python3 -m pip install git-filter-repo
```

## 3. Проверка после force-push

В новом clone:

```bash
git ls-files .env 'data/*' 'logs/*' '*.log' '*.db' '*.pyc' '__pycache__/*' '.DS_Store'
git log --all -- .env
```

Обе команды не должны показывать секретные/runtime-файлы.

## 4. После очистки

1. Создать новый clean clone.
2. Скопировать локальный `.env` из безопасного хранилища, не из Git.
3. Проверить запуск на Bybit demo/testnet.
4. Запретить push секретов pre-commit hook или secret scanning в CI.
