#!/bin/bash
# deploy.sh
# Roda esse script na VPS sempre que precisar atualizar o whatsapp-painel-disparo.
# Uso: bash deploy.sh

set -e
cd /opt/whatsapp-painel-disparo

echo "1/4 - Buscando codigo novo do GitHub..."
git fetch origin

echo "2/4 - Sincronizando com origin/main..."
git reset --hard origin/main
echo "    -> commit atual: $(git log --oneline -1)"

echo "3/4 - Buildando imagem nova..."
docker build --no-cache -t whatsapp-painel-disparo:latest .

echo "4/4 - Atualizando servico..."
docker service update \
    --image whatsapp-painel-disparo:latest \
    --update-order start-first \
    --update-parallelism 1 \
    --update-delay 5s \
    --force whatsapp-painel-disparo

echo ""
echo "PRONTO! Testa em https://disparo.esteiradeleads.shop"
