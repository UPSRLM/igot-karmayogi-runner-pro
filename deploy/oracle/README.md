# Oracle Deployment Scripts

These scripts automate most of the Oracle Always Free deployment steps for this repo.

## Files

- `bootstrap-vm.sh`: installs Docker, Nginx, and Certbot on a fresh Ubuntu VM
- `deploy-app.sh`: starts the Docker Compose service and checks `/healthz`
- `install-nginx-site.sh`: writes the Nginx reverse proxy config for your chosen hostname

## Typical Flow

Run on the Oracle VM after cloning this repo:

```bash
chmod +x deploy/oracle/*.sh
./deploy/oracle/bootstrap-vm.sh
```

Log out and log back in if the script says your user was added to the Docker group.

Then:

```bash
cp .env.example .env
nano .env
./deploy/oracle/deploy-app.sh
./deploy/oracle/install-nginx-site.sh igot.echonerve.com
sudo certbot --nginx -d igot.echonerve.com
```

## Required Manual Inputs

- Oracle account and VM creation
- DNS `A` record for `igot.echonerve.com`
- a strong value for `IGOT_SERVICE_TOKEN`
