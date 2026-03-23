# Call Categorization API (Prototype)

FastAPI service that accepts WAV calls via multipart upload, transcribes with Azure Speech, classifies with Azure OpenAI, and returns one JSON response.

Architecture and operational notes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Production deployment runbook: [docs/DEPLOYMENT_RHEL9_PROD.md](docs/DEPLOYMENT_RHEL9_PROD.md).

Deployment templates:
- `deployment/systemd/calls-category-api.service`
- `deployment/nginx/calls-category-api.conf`
- `deployment/.env.prod.example`
- `deployment/deploy.sh`

## Implemented

- `POST /v1/calls/process` (multipart `file` upload)
- WAV validation (`.wav`, PCM WAV header check, duration and size limits)
- Audio normalization with `ffmpeg` to mono 16kHz 16-bit PCM when available
- Azure Speech STT with configurable language list (`STT_LANGUAGES`, default `uk-UA`)
- Azure OpenAI call classification from taxonomy (`categories.yaml`)
- Strict JSON validation + one repair attempt for malformed model output
- Bearer token auth (`Authorization: Bearer <token>`)
- Unified JSON error responses (`error_code`, `message`, `call_id`)
- Retry/backoff and timeout handling for Azure Speech/OpenAI upstream failures
- Concurrency cap (`MAX_CONCURRENT_CALLS`) to protect service under load
- Verbose dev logging switch (`VERBOSE_AI_LOGS`) with rotating log file (`logs/calls_category_api.log` by default)

## API Request

`POST /v1/calls/process`

Multipart form fields:

- `file` (required): WAV file
- `call_id` (optional): client-provided id
- `metadata` (optional): JSON string object
- `return_transcript_segments` (optional, default `false`)
- `include_extras` (optional, default `true`)

## Quick Start

1. Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Configure env:

```bash
cp .env.example .env
```

Then fill real Azure keys and token.
`API_BEARER_TOKEN` is required; the service fails fast at startup if missing.
`ffmpeg` is optional; if it is missing, or if `ENABLE_FFMPEG=false`, the service can still process compatible mono PCM16 WAV files directly.

Note: code supports both the new env names (`AZURE_*`) and your current legacy names (`SS_*`, `GPT_*`) for compatibility.

3. Run API:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

4. Test endpoint:

```bash
curl -X POST "http://localhost:8000/v1/calls/process" \
  -H "Authorization: Bearer replace-with-strong-secret" \
  -F "file=@wav/8kHz/4min.wav" \
  -F "call_id=test-call-1" \
  -F 'metadata={"operator_id":"42","queue":"general"}' \
  -F "return_transcript_segments=true" \
  -F "include_extras=true"
```

## Healthcheck

`GET /healthz`

## Deployment (RHEL 9)

Automated one-shot deploy:

```bash
sudo ./deployment/deploy.sh
```

If you only want app + systemd (without nginx):

```bash
sudo ./deployment/deploy.sh --skip-nginx
```

1. Prepare app path and env file:

```bash
sudo mkdir -p /opt/calls_category_api /etc/calls-category-api
sudo cp -r . /opt/calls_category_api
sudo cp /opt/calls_category_api/deployment/.env.prod.example /etc/calls-category-api/calls-category-api.env
sudo chown -R callsapi:callsapi /opt/calls_category_api
sudo chmod 600 /etc/calls-category-api/calls-category-api.env
```

2. Install and start systemd service:

```bash
sudo cp /opt/calls_category_api/deployment/systemd/calls-category-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now calls-category-api
sudo systemctl status calls-category-api
```

3. Install nginx reverse proxy config:

```bash
sudo cp /opt/calls_category_api/deployment/nginx/calls-category-api.conf /etc/nginx/conf.d/
sudo nginx -t
sudo systemctl reload nginx
```

Edit placeholders first:
- service user/group and paths in systemd unit
- DNS name and TLS cert/key paths in nginx config
- secrets and Azure settings in env file
