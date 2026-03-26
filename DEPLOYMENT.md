# Condottiere Deployment (VPS)

This guide is for a production-style install using:
- dedicated service user (`condottiere`)
- `pyenv` + Python `3.12.12`
- PostgreSQL on loopback only
- nginx reverse proxy
- user-scope systemd units

## 1. Admin Session: System Prereqs

Run these as an admin user with `sudo` access:

Ubuntu/Debian example:

```bash
sudo apt update
sudo apt install -y \
  git curl build-essential ca-certificates \
  libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
  libffi-dev liblzma-dev tk-dev \
  postgresql postgresql-contrib \
  nginx certbot python3-certbot-nginx
```

Create the service user:

```bash
sudo adduser --disabled-password --gecos "" condottiere
sudo loginctl enable-linger condottiere
sudo systemctl start user@$(id -u condottiere).service
```

## 2. Admin Session: PostgreSQL

Still in admin shell:

Create DB role + DB:

```bash
sudo -u postgres psql -c "CREATE USER condottiere WITH PASSWORD 'CHANGE_ME';"
sudo -u postgres psql -c "CREATE DATABASE condottiere OWNER condottiere;"
```

Lock PostgreSQL to local loopback in `postgresql.conf`:
- `listen_addresses = '127.0.0.1'`

Then restart PostgreSQL:

```bash
sudo systemctl restart postgresql
```

## 3. Service User Session: App Install + Config + systemd

Switch to service user once, then run all steps in this section:

```bash
sudo -iu condottiere
```

Clone repo:

```bash
cd ~
git clone https://github.com/Eadrom/condottiere.git
cd ~/condottiere
```

Install pyenv + Python + project deps:

```bash
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
command -v pyenv >/dev/null 2>&1 || curl https://pyenv.run | bash
eval "$(pyenv init -)"
eval "$(pyenv virtualenv-init -)"
pyenv install -s 3.12.12
pyenv virtualenv -f 3.12.12 condottiere
pyenv local condottiere
python -m pip install --upgrade pip
pip install -e ".[prod]"
```

Configure `.env`:

```bash
cp .env.example .env
```

Edit `~/condottiere/.env`:
- set `ENV=prod`
- set `EVE_CLIENT_ID`, `EVE_CLIENT_SECRET`, `EVE_REDIRECT_BASE`
- set strong `SESSION_SECRET`, `CSRF_SECRET`
- set `FERNET_KEY`
- set `ADMIN_CHARACTER_IDS`
- switch database URL:
  - comment SQLite line
  - uncomment PostgreSQL line and set password

Secret generation examples:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Optional (primary collector only): set `TELEMETRY_PRIMARY_NODE=true`.

Run migrations / upgrade:

```bash
python scripts/update_software.py
```

Install user-scope systemd unit symlinks:

```bash
mkdir -p ~/.config/systemd/user
ln -sf ~/condottiere/systemd/user/condottiere-web.service ~/.config/systemd/user/condottiere-web.service
ln -sf ~/condottiere/systemd/user/condottiere-poller.service ~/.config/systemd/user/condottiere-poller.service
ln -sf ~/condottiere/systemd/user/condottiere-poller.timer ~/.config/systemd/user/condottiere-poller.timer
ln -sf ~/condottiere/systemd/user/condottiere-sender.service ~/.config/systemd/user/condottiere-sender.service
ln -sf ~/condottiere/systemd/user/condottiere-sender.timer ~/.config/systemd/user/condottiere-sender.timer
exit
```

Back in admin shell, load and enable units through the `condottiere` user manager:

```bash
sudo systemctl --user -M condottiere@ daemon-reload
sudo systemctl --user -M condottiere@ enable --now condottiere-web.service condottiere-poller.timer condottiere-sender.timer
```

## 4. Admin Session: nginx + TLS

Back in admin shell.

Important:
- `deploy/nginx/condottiere.conf.example` is tracked in git
- create a local `deploy/nginx/condottiere.conf` from that example
- do not symlink the repo file directly into `/etc/nginx`
- certbot will edit the live nginx file, and that should not create git conflicts in the app checkout

Create your local nginx config from the example:

```bash
cp /home/condottiere/condottiere/deploy/nginx/condottiere.conf.example /home/condottiere/condottiere/deploy/nginx/condottiere.conf
editor /home/condottiere/condottiere/deploy/nginx/condottiere.conf
```

At minimum, update:
- `server_name`

Then copy that local file into nginx:

```bash
sudo cp /home/condottiere/condottiere/deploy/nginx/condottiere.conf /etc/nginx/sites-available/condottiere
```

Enable the site:

```bash
sudo ln -sf /etc/nginx/sites-available/condottiere /etc/nginx/sites-enabled/condottiere
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

TLS with Let’s Encrypt:

```bash
sudo certbot --nginx -d YOUR_DOMAIN --redirect
```

After certbot:
- it is expected that `/etc/nginx/sites-available/condottiere` will now differ from the repo template
- leave `deploy/nginx/condottiere.conf.example` unchanged
- your local `deploy/nginx/condottiere.conf` is not tracked by git
- if upstream nginx template changes later, compare manually:

```bash
diff -u /home/condottiere/condottiere/deploy/nginx/condottiere.conf.example /etc/nginx/sites-available/condottiere
```

## 5. Verify

```bash
curl -sS http://127.0.0.1:8000/status/health
sudo systemctl --user -M condottiere@ status condottiere-web.service --no-pager
sudo systemctl --user -M condottiere@ status condottiere-poller.timer condottiere-sender.timer --no-pager
```

Expected:
- web listens on `127.0.0.1:8000` only
- PostgreSQL listens on `127.0.0.1` only
- public access goes through nginx only

## Update Process

Run update as service user, then restart web from admin shell:

```bash
sudo -iu condottiere
cd ~/condottiere
git pull
pip install -e ".[prod]"
python scripts/update_software.py
exit
sudo systemctl --user -M condottiere@ restart condottiere-web.service
```
