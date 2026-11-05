#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then echo 'أنشئ .env أولاً'; exit 1; fi
if [ ! -x .venv/bin/python ]; then echo 'نفّذ install_termux.sh أولاً'; exit 1; fi
mkdir -p logs
tmux has-session -t smart-trading-bot 2>/dev/null && { echo 'الجلسة تعمل بالفعل'; exit 0; }
command="while true; do echo \"[\$(date -Is)] starting bot\"; .venv/bin/python bot.py; code=\$?; echo \"[\$(date -Is)] bot exited with code \$code\"; if [ \"\$code\" -eq 0 ]; then break; fi; sleep 10; done"
tmux new-session -d -s smart-trading-bot "cd '$PWD' && $command >> logs/bot.log 2>&1"
echo 'بدأت جلسة tmux: smart-trading-bot'
