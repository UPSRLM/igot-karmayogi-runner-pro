#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <server-name>"
  echo "Example: $0 igot.echonerve.com"
  exit 1
fi

SERVER_NAME="$1"
SITE_PATH="/etc/nginx/sites-available/igot-qa-runner"
LINK_PATH="/etc/nginx/sites-enabled/igot-qa-runner"

sudo tee "${SITE_PATH}" > /dev/null <<EOF
server {
    listen 80;
    server_name ${SERVER_NAME};

    client_max_body_size 50m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

if [[ ! -L "${LINK_PATH}" ]]; then
  sudo ln -s "${SITE_PATH}" "${LINK_PATH}"
fi

sudo nginx -t
sudo systemctl reload nginx
echo "Nginx site installed for ${SERVER_NAME}."