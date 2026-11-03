#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then echo 'أنشئ .env أولاً'; exit 1; fi
if [ ! -x .venv/bin/python ]; then echo 'نفّذ install_termux.sh أولاً'; exit 1; fi
tmux has-session -t smart-trading-bot 2>/dev/null && { echo 'الجلسة تعمل بالفعل'; exit 0; }
tmux new-session -d -s smart-trading-bot "cd '$PWD' && .venv/bin/python bot.py >> logs/bot.log 2>&1"
echo 'بدأت جلسة tmux: smart-trading-bot'
