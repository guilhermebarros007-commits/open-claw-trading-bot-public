
$VPS = "root@72.60.146.212"
$REMOTE_DIR = "/opt/trading-agents"
$LOCAL_BACKEND = "c:\Users\guilh\PROJETO OPEN-CLAW (CLAUDE)\backend"

Write-Host "🚀 Iniciando deploy via PowerShell para $VPS..." -ForegroundColor Cyan

# 1. Empacotar localmente (exclui .env, data.db, caches)
Write-Host "Criando pacote de atualizacao..."
if (Test-Path "deploy.tar.gz") { Remove-Item "deploy.tar.gz" }
# Usando tar do Windows
tar -czvf deploy.tar.gz -C "$LOCAL_BACKEND" . --exclude=".env" --exclude="data.db" --exclude="__pycache__" --exclude=".git" --exclude="*.log"

# 2. Preparar diretório remoto (limpar arquivos antigos exceto configurações)
Write-Host "Limpando arquivos antigos no VPS..."
ssh $VPS "mkdir -p $REMOTE_DIR && find $REMOTE_DIR -mindepth 1 -maxdepth 1 -not -name '.env' -not -name 'data.db' -not -name '.venv' -exec rm -rf {} +"

# 3. Transferir pacote
Write-Host "Enviando pacote para o VPS..."
scp deploy.tar.gz "$($VPS):$REMOTE_DIR/deploy.tar.gz"

# 4. Extrair e reiniciar
Write-Host "Extraindo e reiniciando servico..."
ssh $VPS "cd $REMOTE_DIR && tar -xzvf deploy.tar.gz && rm deploy.tar.gz && systemctl restart trading-agents"

# 5. Sincronizar .env (opcional, mas seguro se houve mudanças)
Write-Host "Sincronizando .env..."
scp "$LOCAL_BACKEND\.env" "$($VPS):$REMOTE_DIR/.env"

# 6. Finalizar
if (Test-Path "deploy.tar.gz") { Remove-Item "deploy.tar.gz" }
Write-Host "Deploy concluido com sucesso!"
ssh $VPS "systemctl status trading-agents --no-pager -l | head -n 15"
