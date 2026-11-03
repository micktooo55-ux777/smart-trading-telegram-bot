#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
tmux kill-session -t smart-trading-bot 2>/dev/null || true
echo 'تم إيقاف البوت'
