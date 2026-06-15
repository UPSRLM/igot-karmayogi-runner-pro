# Oracle Always Free Deployment

This is the lowest-cost cloud path that can realistically run this project.

Use this when you want:

- WordPress to stay on `echonerve.com`
- the Python + Playwright worker to run on a separate server
- a public backend URL such as `https://igot.echonerve.com`

## What To Create

Create an Oracle Cloud Always Free compute instance with these settings:

- Shape: `VM.Standard.A1.Flex`
- OCPU: `2`
- Memory: `12 GB`
- Image: `Ubuntu 22.04` or `Ubuntu 24.04` for `aarch64`
- Boot volume: default is fine

Why this shape:

- `E2.1.Micro` is usually too small for Playwright + Chromium
- Ampere A1 gives enough RAM for one browser job at a time
- this repo already uses the Playwright Docker image, which is the cleanest deployment path

## Network Rules

In the Oracle instance security rules, allow:

- TCP `22` from your IP only
- TCP `80` from `0.0.0.0/0`
- TCP `443` from `0.0.0.0/0`

Do not expose `8080` publicly unless you are debugging.

## DNS

Create an `A` record:

- Host: `igot`
- Value: your Oracle VM public IP

After the DNS record is added, `igot.echonerve.com` should point to the VM.

## Server Bootstrap

SSH into the instance, then run:

Quickest path from this repo after cloning it on the VM:

```bash
chmod +x deploy/oracle/*.sh
./deploy/oracle/bootstrap-vm.sh
```

Manual equivalent:

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y ca-certificates curl git nginx certbot python3-certbot-nginx

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
```

Log out and SSH back in so the Docker group change is applied.

## App Deployment

Clone the repo on the VM:

```bash
cd ~
git clone <your-repo-url> live_igot_qa_1
cd live_igot_qa_1
```

Create the environment file:

```bash
cp .env.example .env
```

Edit `.env` and set a strong token:

```env
IGOT_SERVICE_TOKEN=replace-with-a-long-random-secret
IGOT_SERVICE_HOST=0.0.0.0
IGOT_SERVICE_PORT=8080
IGOT_BASE_URL=https://portal.igotkarmayogi.gov.in
IGOT_SERVICE_DATA_ROOT=/app/service-data
IGOT_SERVICE_REPORTS_ROOT=/app/reports
IGOT_SERVICE_PROFILE_ROOT=/app/browser-profile
IGOT_PYTHON_EXECUTABLE=python
```

Start the service:

Fast path:

```bash
./deploy/oracle/deploy-app.sh
```

Manual equivalent:

```bash
docker compose up -d --build
```

Verify the container:

```bash
docker compose ps
curl http://127.0.0.1:8080/healthz
```

Expected result:

```json
{"status":"ok"}
```

## Reverse Proxy

Create the Nginx site config:

Fast path:

```bash
./deploy/oracle/install-nginx-site.sh igot.echonerve.com
```

Manual equivalent:

```bash
sudo tee /etc/nginx/sites-available/igot <<'EOF'
server {
    listen 80;
    server_name igot.echonerve.com;

    client_max_body_size 50m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

sudo ln -s /etc/nginx/sites-available/igot /etc/nginx/sites-enabled/igot
sudo nginx -t
sudo systemctl reload nginx
```

## HTTPS

Once DNS is pointing correctly, issue the certificate:

```bash
sudo certbot --nginx -d igot.echonerve.com
```

Then verify:

```bash
curl https://igot.echonerve.com/healthz
```

Expected result:

```json
{"status":"ok"}
```

## WordPress Plugin Configuration

After the backend is live, in WordPress set:

- API Base URL: `https://igot.echonerve.com`
- API Bearer Token: the exact value of `IGOT_SERVICE_TOKEN`

Then submit a small test run with:

- `Max Modules`: `1`
- one valid `Start URL` or `Course URL`

## Operations

Useful commands on the VM:

```bash
cd ~/live_igot_qa_1
docker compose logs -f
docker compose restart
docker compose pull
docker compose up -d --build
```

## Known Tradeoffs

- Always Free capacity is not guaranteed in every region
- browser automation will run, but keep it to one job at a time
- the first login/bootstrap step may still need manual attention inside the browser profile
- if Oracle capacity is unavailable, use a cheap VPS instead of spending time fighting provisioning limits
