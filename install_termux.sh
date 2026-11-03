#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
mkdir -p logs data
chmod 700 .env 2>/dev/null || true
echo "تم التثبيت. أنشئ .env ثم نفّذ: bash run_tmux.sh"
