# Manual Production Deployment Guide for RHEL 9

This document describes how to deploy the Call Categorization API manually on a Red Hat Enterprise Linux 9 server without using `deployment/deploy.sh`.

Use this guide if the server administrator wants to create every directory, file, service, and configuration manually.

## 1. Target layout

This guide assumes the final production layout will be:

- application code: `/opt/calls_category_api`
- Python virtual environment: `/opt/calls_category_api/.venv`
- application logs: `/opt/calls_category_api/logs`
- environment file: `/etc/calls-category-api/calls-category-api.env`
- systemd unit: `/etc/systemd/system/calls-category-api.service`
- nginx config: `/etc/nginx/conf.d/calls-category-api.conf`

Traffic flow:

```text
Server A
  -> HTTPS 443
nginx on Server B
  -> localhost:8000
uvicorn / FastAPI
  -> HTTPS 443
Azure Speech + Azure OpenAI
```

## 2. Required information before starting

The administrator needs all of the following before deployment:

- SSH access with `sudo`
- GitHub repository URL
- Azure Speech key and region
- Azure OpenAI endpoint, key, deployment, and API version
- a strong `API_BEARER_TOKEN`
- internal DNS name for the API
- TLS certificate and private key for that DNS name
- approved firewall/network access:
  - inbound `443/TCP` from Server A to Server B
  - outbound `443/TCP` to Azure OpenAI and Azure Speech
  - outbound DNS access
  - outbound package repository access
  - outbound `443/TCP` to `github.com` if cloning from GitHub
  - outbound `443/TCP` to `pypi.org` and `files.pythonhosted.org`

## 3. Install required OS packages

Install the base packages:

```bash
sudo dnf install -y git python3 python3-pip nginx curl
```

Optional but recommended:

- install `ffmpeg` from the company-approved repository or internal package mirror

Notes:

- `ffmpeg` is optional
- if `ffmpeg` is missing, the API can still process compatible direct STT WAV files
- if you want to force-disable `ffmpeg` even when it is installed, use `ENABLE_FFMPEG=false` in the environment file

## 4. Create the service user and required directories

Create the service group:

```bash
sudo groupadd --system callsapi
```

Create the service user:

```bash
sudo useradd \
  --system \
  --gid callsapi \
  --home-dir /opt/calls_category_api \
  --shell /sbin/nologin \
  callsapi
```

Create the base directories:

```bash
sudo mkdir -p /opt
sudo mkdir -p /etc/calls-category-api
```

## 5. Clone the code from GitHub

Clone the repository into `/opt`:

```bash
cd /opt #TODO: END HERE
sudo git clone https://github.com/<ORG>/<REPO>.git calls_category_api
```

If your repository is private and your company uses SSH-based deploy keys, use the approved GitHub SSH method instead.

Create the logs directory and set ownership:

```bash
sudo mkdir -p /opt/calls_category_api/logs
sudo chown -R callsapi:callsapi /opt/calls_category_api
```

## 6. Create the Python virtual environment

Create the virtual environment:

```bash
sudo python3 -m venv /opt/calls_category_api/.venv
```

Upgrade `pip`:

```bash
sudo /opt/calls_category_api/.venv/bin/python -m pip install --upgrade pip
```

Install Python dependencies:

```bash
sudo /opt/calls_category_api/.venv/bin/pip install -r /opt/calls_category_api/requirements.txt
```

## 7. Create the production environment file

Create the environment file:
#TODO: .env ENV LOCATION!!!!!!
```bash
sudo vi /etc/calls-category-api/calls-category-api.env 
```

Put this content into the file and replace the placeholder values:

```env
AZURE_SPEECH_KEY=replace-me
AZURE_SPEECH_REGION=replace-me

AZURE_OPENAI_ENDPOINT=https://replace-me.openai.azure.com
AZURE_OPENAI_API_KEY=replace-me
AZURE_OPENAI_DEPLOYMENT=gpt-4o
AZURE_OPENAI_API_VERSION=2024-02-15-preview

# Optional. Set these only if the server must use a corporate outbound proxy.
HTTP_PROXY=http://proxy.example.local:3128
HTTPS_PROXY=http://proxy.example.local:3128
NO_PROXY=127.0.0.1,localhost,.example.local
http_proxy=http://proxy.example.local:3128
https_proxy=http://proxy.example.local:3128
no_proxy=127.0.0.1,localhost,.example.local

API_BEARER_TOKEN=replace-with-long-random-secret

MAX_UPLOAD_MB=25
MAX_DURATION_MINUTES=20
PROMPT_VERSION=1
TAXONOMY_PATH=/opt/calls_category_api/categories.yaml

FFMPEG_BINARY=/usr/bin/ffmpeg
ENABLE_FFMPEG=true
STT_LANGUAGES=uk-UA

MAX_CONCURRENT_CALLS=2
OPENAI_TIMEOUT_SECONDS=60
OPENAI_MAX_ATTEMPTS=3
OPENAI_RETRY_BASE_DELAY_MS=500
SPEECH_TIMEOUT_SECONDS=3600
SPEECH_MAX_ATTEMPTS=2
SPEECH_RETRY_BASE_DELAY_MS=500

LOG_LEVEL=INFO
LOG_FILE=/opt/calls_category_api/logs/calls_category_api.log
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5
LOG_TRANSCRIPTS=false
VERBOSE_AI_LOGS=false
```

Important notes:
#FIXME:!!!
- keep `LOG_TRANSCRIPTS=false`
- keep `VERBOSE_AI_LOGS=false`
- set `ENABLE_FFMPEG=false` only if you intentionally want to bypass normalization
- if `ffmpeg` is not installed, you can still leave `FFMPEG_BINARY=/usr/bin/ffmpeg`; the app will warn and skip normalization
- for your original call format, direct processing works when the WAV is mono PCM16 at `8kHz` or `16kHz`
- if your server reaches the internet only through a proxy, set the `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` values here before starting the service

Protect the environment file:

```bash
sudo chown root:root /etc/calls-category-api/calls-category-api.env
sudo chmod 600 /etc/calls-category-api/calls-category-api.env
```

## 8. Create the systemd service file

Create the service file:

```bash
sudo vi /etc/systemd/system/calls-category-api.service
```

Put this content into the file:

```ini
[Unit]
Description=Call Categorization API (FastAPI/Uvicorn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=callsapi
Group=callsapi
WorkingDirectory=/opt/calls_category_api
EnvironmentFile=/etc/calls-category-api/calls-category-api.env
ExecStart=/opt/calls_category_api/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=5
TimeoutStopSec=60
KillSignal=SIGINT
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/opt/calls_category_api/logs
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

The service reads all application settings, including optional proxy variables, from `/etc/calls-category-api/calls-category-api.env`.

Reload systemd:
#TODO end here!!!
```bash
sudo systemctl daemon-reload
```

Enable the service:

```bash
sudo systemctl enable calls-category-api
```

## 9. Create the nginx reverse proxy config

Create the nginx config:

```bash
sudo vi /etc/nginx/conf.d/calls-category-api.conf
```

Put this content into the file and replace the placeholders:

```nginx
upstream calls_category_api_upstream {
    server 127.0.0.1:8000;
    keepalive 32;
}

server {
    listen 443 ssl http2;
    server_name calls-category-api.internal.company;

    ssl_certificate /etc/pki/tls/certs/calls-category-api.crt;
    ssl_certificate_key /etc/pki/tls/private/calls-category-api.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;

    client_max_body_size 30m;
    client_body_timeout 60s;

    location / {
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";

        proxy_connect_timeout 10s;
        proxy_send_timeout 3900s;
        proxy_read_timeout 3900s;

        proxy_pass http://calls_category_api_upstream;
    }
}
```

Validate nginx config:

```bash
sudo nginx -t
```

Enable nginx:

```bash
sudo systemctl enable nginx
```

## 10. Start the application and nginx

Start the API service:

```bash
sudo systemctl start calls-category-api
```

Check status:

```bash
sudo systemctl status calls-category-api
```

Start nginx:

```bash
sudo systemctl start nginx
```

Check status:

```bash
sudo systemctl status nginx
```

## 11. Validate the deployment

Check the application logs:

```bash
sudo journalctl -u calls-category-api -n 100 --no-pager
sudo tail -n 100 /opt/calls_category_api/logs/calls_category_api.log
```
#TODO: here everyting broken!!!!!

Check local health:

```bash
curl http://127.0.0.1:8000/healthz
```

Expected response:

```json
{"status":"ok"}
```

Check HTTPS through nginx:

```bash
curl -k https://calls-category-api.internal.company/healthz
```

If the certificate is trusted by the server, `-k` is not required.

## 12. Run one production smoke test

From a trusted host that can reach the server:

```bash
curl -X POST "https://calls-category-api.internal.company/v1/calls/process" \
  -H "Authorization: Bearer <REAL_BEARER_TOKEN>" \
  -F "file=@wav/8kHz/4min.wav" \
  -F "call_id=prod-manual-smoke-1" \
  -F "include_extras=true"
```

Expected result:

- HTTP `200`
- JSON response with `transcription`, `classification`, and `timings_ms`

## 13. Manual update procedure

Go to the application directory:

```bash
cd /opt/calls_category_api
```

Pull the latest code:

```bash
sudo git pull
```

Reinstall Python dependencies if needed:

```bash
sudo /opt/calls_category_api/.venv/bin/pip install -r /opt/calls_category_api/requirements.txt
```

Restart the service:

```bash
sudo systemctl restart calls-category-api
sudo systemctl restart nginx
```

Re-check status:

```bash
sudo systemctl status calls-category-api
sudo systemctl status nginx
```

## 14. Manual deployment checklist

Before handing the server over, confirm all of the following:

- `uvicorn` listens only on `127.0.0.1:8000`
- `nginx` exposes only `443`
- the API is reachable only from approved internal networks
- the environment file is mode `600`
- the app runs as `callsapi`
- `LOG_LEVEL=INFO`
- `LOG_TRANSCRIPTS=false`
- `VERBOSE_AI_LOGS=false`
- `API_BEARER_TOKEN` is strong and random
- `ENABLE_FFMPEG=true` unless you intentionally want direct `8kHz` processing
- if `ffmpeg` is installed, `/usr/bin/ffmpeg -version` works

## 15. Common failure cases

If `calls-category-api` does not start:

```bash
sudo journalctl -u calls-category-api -n 100 --no-pager
```

Common causes:

- wrong Azure credentials
- wrong Azure endpoint
- blocked outbound network access
- missing Python dependency
- uploaded WAV is not directly compatible and `ENABLE_FFMPEG=false`
- uploaded WAV is not directly compatible and `ffmpeg` is not installed

If nginx does not start:

- check `sudo nginx -t`
- confirm TLS certificate and key paths are correct
- confirm `server_name` is correct

If the app works locally but not from Server A:

- check firewall rules
- check DNS
- check TLS trust
- check Bearer token
