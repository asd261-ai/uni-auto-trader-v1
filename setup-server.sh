#!/bin/bash
# 在 VPS 上執行這個腳本來完整設定環境
set -e

echo "=== 1. 更新系統 ==="
sudo apt update -y && sudo apt install -y python3.11 python3.11-venv python3-pip git

echo "=== 2. Clone repo ==="
cd ~
git clone https://github.com/asd261-ai/uni-auto-trader-v1.git || (cd uni-auto-trader-v1 && git pull)
cd uni-auto-trader-v1

echo "=== 3. 建立虛擬環境 ==="
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "=== 4. 設定 systemd service ==="
sudo tee /etc/systemd/system/uni-trader.service > /dev/null << 'EOF'
[Unit]
Description=Uni Auto Trader
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/uni-auto-trader-v1
ExecStart=/home/ubuntu/uni-auto-trader-v1/.venv/bin/python main.py
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable uni-trader

echo ""
echo "=== 完成！==="
echo "接下來："
echo "  1. scp .env 和 .pfx 憑證上傳到 ~/uni-auto-trader-v1/"
echo "  2. sudo systemctl start uni-trader"
echo "  3. sudo journalctl -u uni-trader -f  # 看 log"
