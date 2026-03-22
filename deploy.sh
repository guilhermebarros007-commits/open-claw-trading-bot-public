#!/bin/bash
# ── Deploy: Trading Agents → VPS 72.60.146.212 ────────────────────────────
# Uso: bash deploy.sh
# Pré-requisito: chave SSH em ~/.ssh/id_ed25519

set -e
VPS="root@72.60.146.212"
REMOTE_DIR="/opt/trading-agents"
LOCAL_DIR="$(cd "$(dirname "$0")/backend" && pwd)"

echo "🚀 Iniciando deploy para $VPS..."

# ── 1. Cria pasta no VPS ───────────────────────────────────────────────────
ssh "$VPS" "mkdir -p $REMOTE_DIR"

# ── 2. Sincroniza arquivos (exclui .env, __pycache__, data.db) ─────────────
rsync -avz --progress --delete \
  --exclude='.env' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='data.db' \
  --exclude='.git' \
  "$LOCAL_DIR/" "$VPS:$REMOTE_DIR/"

# ── 3. Copia .env separadamente (contém chaves) ────────────────────────────
scp "$LOCAL_DIR/.env" "$VPS:$REMOTE_DIR/.env"

# ── 4. Instala dependências no VPS ─────────────────────────────────────────
ssh "$VPS" bash << 'REMOTE'
  cd /opt/trading-agents
  python3 -m venv .venv 2>/dev/null || true
  source .venv/bin/activate
  pip install -q --upgrade pip
  pip install -q -r requirements.txt
  echo "✅ Dependências instaladas"
REMOTE

# ── 5. Cria serviço systemd ────────────────────────────────────────────────
ssh "$VPS" bash << 'REMOTE'
cat > /etc/systemd/system/trading-agents.service << 'EOF'
[Unit]
Description=Trading Agents (FastAPI)
After=network.target

[Service]
WorkingDirectory=/opt/trading-agents
ExecStart=/opt/trading-agents/.venv/bin/python run.py
Restart=always
RestartSec=10
EnvironmentFile=/opt/trading-agents/.env
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable trading-agents
systemctl restart trading-agents
echo "✅ Serviço systemd ativo"
REMOTE

# ── 6. Abre porta 8000 no firewall ─────────────────────────────────────────
ssh "$VPS" "ufw allow 8000/tcp 2>/dev/null || iptables -I INPUT -p tcp --dport 8000 -j ACCEPT || true"

# ── 7. Verifica status ─────────────────────────────────────────────────────
sleep 3
ssh "$VPS" "systemctl status trading-agents --no-pager -l | head -20"

echo ""
echo "✅ Deploy concluído!"
echo "🌐 Acesse: http://72.60.146.212:8000"
