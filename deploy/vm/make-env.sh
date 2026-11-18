#!/bin/sh
# Create .env with fresh random secrets (never printed). Usage: sh deploy/vm/make-env.sh app.example.com [demo]
set -eu
[ -f .env ] && { echo ".env already exists; not overwriting"; exit 0; }
umask 077
cp .env.example .env
sed -i "s|^NULLWAKE_DOMAIN=.*|NULLWAKE_DOMAIN=$1|" .env
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" .env
sed -i "s|^DEMO_READER_PASSWORD=.*|DEMO_READER_PASSWORD=$(openssl rand -hex 24)|" .env
if [ "${2:-}" = "demo" ]; then sed -i "s|^NULLWAKE_DEMO_MODE=.*|NULLWAKE_DEMO_MODE=true|" .env; fi
echo ".env written for $1"
