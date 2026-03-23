# Call Categorization API Architecture

## Purpose

This service receives WAV call recordings over HTTP, transcribes them with Azure Speech, classifies them with Azure OpenAI, and returns one JSON response with transcript, labels, extras, and timings.

## Scope

- Ingestion mode: multipart upload (`POST /v1/calls/process`).
- Storage: no Azure Storage dependency, request-scoped temp files only.
- Languages: configurable STT auto-detection (`STT_LANGUAGES`, default `uk-UA`).
- Audio input: WAV expected; normalized internally to mono 16kHz PCM16 before STT when `ffmpeg` is enabled and available.

## High-Level Components

- `app/main.py`
  - FastAPI app, endpoint handlers, auth guard, middleware, orchestration pipeline.
- `app/audio.py`
  - Upload type checks, WAV header inspection, optional ffmpeg normalization, direct-input fallback.
- `app/speech.py`
  - Azure Speech transcription service with retries and timeout mapping.
- `app/classifier.py`
  - Azure OpenAI classification service, strict JSON validation, repair call fallback.
- `app/taxonomy.py`
  - Taxonomy loader and prompt formatting from `categories.yaml`.
- `app/models.py`
  - Response/error schemas.
- `app/errors.py`
  - Unified API exception hierarchy and HTTP status mapping.
- `app/config.py`
  - Environment-driven settings and derived properties.
- `app/logging_setup.py`
  - Console + rotating file logging configuration.

## Request Flow

```text
Server A
  |
  | 1) POST /v1/calls/process (multipart WAV + metadata + bearer token)
  v
FastAPI (main.py)
  |
  | 2) Auth guard + request size checks + metadata parsing
  | 3) Save upload -> temp/input.wav
  | 4) inspect_wav() validation (codec/channels/rate/duration)
  | 5) normalize with ffmpeg when enabled and available, otherwise use compatible source WAV directly
  | 6) SpeechService.transcribe()
  |      -> Azure Speech
  |      <- transcript text + optional segments + detected languages
  | 7) ClassificationService.classify()
  |      -> Azure OpenAI (strict JSON)
  |      <- caller_type + call_category + extras + confidence
  | 8) Build final ProcessCallResponse + timings
  v
Server A receives JSON and persists result
```

## Endpoint Contract

### `POST /v1/calls/process`

Request fields:
- `file` (required): WAV file.
- `call_id` (optional): external call identifier.
- `metadata` (optional): JSON object serialized as string.
- `return_transcript_segments` (optional, default `false`).
- `include_extras` (optional, default `true`).

Response (200):
- `call_id`
- `transcription` (`text`, optional `segments`, optional `detected_languages`, `stt_metadata`)
- `classification` (`caller_type`, `call_category`, confidences, optional `extras`, `model`, `prompt_version`)
- `timings_ms` (`normalize`, `stt`, `clf`, `total`)

Error shape:
- `{ "error_code": "...", "message": "...", "call_id": "..." }`

## Concurrency Model

- Request handler is async, but CPU/blocking work is moved to threadpool (`run_in_threadpool`).
- Global in-process concurrency guard: `asyncio.Semaphore(MAX_CONCURRENT_CALLS)`.
- Temp files are isolated per request via `TemporaryDirectory`.

## Reliability Model

- Startup fail-fast checks:
  - bearer token exists
  - taxonomy loads/validates
- Startup warnings:
  - if `ffmpeg` is missing, normalization is disabled and compatible WAV files are sent directly to STT
  - if `ENABLE_FFMPEG=false`, normalization is intentionally disabled and compatible WAV files are sent directly to STT
- Upstream retries with exponential backoff + jitter:
  - Azure Speech
  - Azure OpenAI
- Upstream errors mapped to API errors:
  - `429` rate-limited
  - `503` unavailable
  - `504` timeout
  - `500` non-retryable/internal failures

## Configuration (Environment)

Core:
- `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`
- `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`
- `API_BEARER_TOKEN`

Processing:
- `MAX_UPLOAD_MB`, `MAX_DURATION_MINUTES`
- `TAXONOMY_PATH`, `PROMPT_VERSION`
- `FFMPEG_BINARY` (optional), `ENABLE_FFMPEG`, `STT_LANGUAGES`

Resilience:
- `MAX_CONCURRENT_CALLS`
- `OPENAI_TIMEOUT_SECONDS`, `OPENAI_MAX_ATTEMPTS`, `OPENAI_RETRY_BASE_DELAY_MS`
- `SPEECH_TIMEOUT_SECONDS`, `SPEECH_MAX_ATTEMPTS`, `SPEECH_RETRY_BASE_DELAY_MS`

Logging:
- `LOG_LEVEL`, `LOG_FILE`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`
- `LOG_TRANSCRIPTS`, `VERBOSE_AI_LOGS`

## Logging and Sensitive Data

- Default production posture:
  - `LOG_LEVEL=INFO`
  - `LOG_TRANSCRIPTS=false`
  - `VERBOSE_AI_LOGS=false`
- `VERBOSE_AI_LOGS=true` is for development only; it can log prompts, transcript text, and model payloads.
- Log file is rotating; permission hardening attempts to set mode `600`.

## Security Model

- Bearer token required for `/v1/calls/process`.
- Expected deployment behind internal reverse proxy with HTTPS/TLS.
- Request size limits enforced via middleware and streamed upload checks.

## Developer Runbook

Run locally:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Health:

```bash
curl http://127.0.0.1:8000/healthz
```

Single call smoke test:

```bash
python3 wav/test.py \
  --token "$API_BEARER_TOKEN" \
  --file wav/8kHz/4min.wav \
  --call-id demo-1
```

Parallel smoke test:

```bash
mkdir -p /tmp/call-load
seq 1 10 | xargs -P 5 -I{} sh -c '
  python3 wav/test.py \
    --token "$API_BEARER_TOKEN" \
    --file wav/8kHz/4min.wav \
    --call-id "load-{}" \
    > /tmp/call-load/load-{}.log 2>&1
  echo $? > /tmp/call-load/load-{}.code
'
```

## Operational Notes for Red Hat 9

- Primary operator runbook: `docs/DEPLOYMENT_RHEL9_PROD.md`
- `ffmpeg` is optional but recommended for best STT quality and broader WAV compatibility.
- Run app as non-root service account.
- Keep `.env` readable only by service user.
- Restrict inbound access to internal network and reverse proxy.
- Allow outbound HTTPS to Azure Speech/OpenAI endpoints.
- Deployment templates are provided in:
  - `deployment/systemd/calls-category-api.service`
  - `deployment/nginx/calls-category-api.conf`
  - `deployment/.env.prod.example`
  - `deployment/deploy.sh` (one-shot installer for RHEL-like hosts)

### Before using on server, customize:

- User/Group, WorkingDirectory, and EnvironmentFile in systemd unit.
- server_name, ssl_certificate, and ssl_certificate_key in nginx config.
- Real Azure keys and strong bearer token in env file.

## Known Limitations

- Sync-only API (no job queue/async endpoint yet).
- No persistent DB storage in current design.
- Liveness endpoint exists; readiness endpoint can be added later.


## How to run autodeploy:

1. First run:
```bash
sudo ./deployment/deploy.sh
```
2. Edit ```/etc/calls-category-api/calls-category-api.env``` with real Azure keys/token.
3. Run again:
```bash
sudo ./deployment/deploy.sh
```
4. Optional app-only deploy (no nginx):
```bash
sudo ./deployment/deploy.sh --skip-nginx
```
5. Optional runtime overrides (example):
```bash
sudo NGINX_SERVER_NAME=api.internal.company \
TLS_CERT_PATH=/etc/pki/tls/certs/api.crt \
TLS_KEY_PATH=/etc/pki/tls/private/api.key \
./deployment/deploy.sh
```
