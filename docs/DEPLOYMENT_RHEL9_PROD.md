# Production Deployment Guide for RHEL 9

This document is for the person who will host the Call Categorization API on a production Red Hat Enterprise Linux 9 server.

It describes the recommended production deployment path for this project:

- `nginx` on port `443`
- `uvicorn` bound to `127.0.0.1:8000`
- `systemd` service named `calls-category-api`
- application files under `/opt/calls_category_api`
- environment file under `/etc/calls-category-api/calls-category-api.env`

The deployment templates used by this guide already exist in the repository:

- `deployment/deploy.sh`
- `deployment/systemd/calls-category-api.service`
- `deployment/nginx/calls-category-api.conf`
- `deployment/.env.prod.example`

## 1. Target architecture

Expected traffic flow:

```text
Server A
  -> HTTPS 443
nginx on Server B
  -> localhost:8000
uvicorn / FastAPI app
  -> HTTPS 443
Azure Speech + Azure OpenAI
```

Important production assumptions:

- The API is internal-only, not public internet-facing.
- `nginx` terminates TLS.
- `uvicorn` must not be exposed directly on the network.
- The Bearer token remains enabled.
- Production logging must not include transcripts or full AI payloads.

## 2. Required information before deployment

Before starting, the administrator must have:

- SSH or console access with `sudo`
- the project source code archive or repository copy
- Azure Speech key and region
- Azure OpenAI endpoint, key, deployment name, and API version
- a strong `API_BEARER_TOKEN`
- internal DNS name for the API, for example `calls-category-api.internal.company`
- TLS certificate and private key for that DNS name
- approved network rules:
  - inbound `443/TCP` from Server A to Server B
  - outbound `443/TCP` from Server B to Azure OpenAI and Azure Speech
  - outbound DNS access to corporate resolvers
  - outbound access to package repositories used during installation

## 3. Required network access

The server team should approve the following network access before installation.

Runtime access:

- inbound `443/TCP` from Server A to Server B
- outbound `443/TCP` from Server B to Azure OpenAI:
  - `https://<your-openai-resource>.openai.azure.com`
- outbound `443/TCP` from Server B to Azure Speech STT:
  - `https://<your-speech-region>.stt.speech.microsoft.com`
- outbound `443/TCP` from Server B to Azure Speech token endpoint:
  - `https://<your-speech-region>.api.cognitive.microsoft.com`
- outbound DNS access from Server B to corporate DNS resolvers

Installation and update access:

- outbound `443/TCP` to package repositories approved by the company
- outbound `443/TCP` to `https://pypi.org`
- outbound `443/TCP` to `https://files.pythonhosted.org`
- outbound `443/TCP` to `https://github.com` only if the server pulls source code directly from GitHub

Preferred approval model:

- exact FQDN allowlist for the real Azure resources
- if the company wants a flexible Azure allowlist for future resource changes:
  - `*.openai.azure.com`
  - `*.stt.speech.microsoft.com`
  - `*.api.cognitive.microsoft.com`

Do not expose `uvicorn` port `8000` externally. It must stay reachable only from localhost.

## 4. Required OS packages

The server needs these tools installed:

- `python3`
- `python3` with `venv` support
- `nginx`
- `curl`
- `rsync`
- `ffmpeg` (optional but recommended)

Example installation approach on RHEL 9:

```bash
sudo dnf install -y python3 python3-pip nginx curl rsync
```

`ffmpeg` is often not available in the default RHEL repository. Install it from the company-approved repository or internal package mirror if possible. The application expects the binary at `/usr/bin/ffmpeg` unless `FFMPEG_BINARY` is overridden in the environment file.

If `ffmpeg` is missing, the API can still work without it, but only for compatible direct STT input files. In this project that means the uploaded WAV should already be mono PCM16 at `8kHz` or `16kHz`. With `ffmpeg` installed, the service can normalize audio automatically and generally gives better STT quality.

You can also force the application not to use `ffmpeg` even if it is installed:

- `ENABLE_FFMPEG=false`

This is useful when you want to guarantee that production uses the original `8kHz` WAV files directly.

## 5. Copy the project to the server

Transfer the project to the target server, for example into the home directory of the administrator account:

```bash
scp -r ./calls_category_api admin@server-b:/home/admin/
```

After login:

```bash
cd /home/admin/calls_category_api
```

The deployment script copies the project into `/opt/calls_category_api`, so the initial location is only temporary.

## 6. First deployment run

Run the deployment script once as root:

```bash
sudo ./deployment/deploy.sh
```

What this first run does:

- creates Linux service user and group `callsapi` if they do not exist
- copies the project into `/opt/calls_category_api`
- creates the Python virtual environment
- installs Python dependencies from `requirements.txt`
- creates `/etc/calls-category-api/calls-category-api.env` from the production template if missing
- writes the `systemd` unit file
- writes the `nginx` config file

If the environment file still contains placeholder values, the script intentionally does not start the service. This is expected behavior.

## 7. Fill the production environment file

Open the generated environment file:

```bash
sudo vi /etc/calls-category-api/calls-category-api.env
```

Fill real values for at least:

- `AZURE_SPEECH_KEY`
- `AZURE_SPEECH_REGION`
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_DEPLOYMENT`
- `AZURE_OPENAI_API_VERSION`
- `API_BEARER_TOKEN`

Review production-safe values:

- `LOG_LEVEL=INFO`
- `LOG_TRANSCRIPTS=false`
- `VERBOSE_AI_LOGS=false`
- `MAX_CONCURRENT_CALLS=2`
- `STT_LANGUAGES=uk-UA`

Recommended production path values:

- `TAXONOMY_PATH=/opt/calls_category_api/categories.yaml`
- `FFMPEG_BINARY=/usr/bin/ffmpeg`
  - if `ffmpeg` is not installed, you can still leave this value as-is; the service will log a warning and skip normalization
- `ENABLE_FFMPEG=true`
  - set `ENABLE_FFMPEG=false` to force-disable normalization and always use the original compatible WAV directly
- `LOG_FILE=/opt/calls_category_api/logs/calls_category_api.log`

Protect the file:

```bash
sudo chmod 600 /etc/calls-category-api/calls-category-api.env
```

## 8. Define DNS name and TLS certificate paths

The deployment script generates the `nginx` configuration from environment overrides passed to the script at runtime.

Use these variables:

- `NGINX_SERVER_NAME`
- `TLS_CERT_PATH`
- `TLS_KEY_PATH`

Example:

```bash
sudo NGINX_SERVER_NAME=calls-category-api.internal.company \
TLS_CERT_PATH=/etc/pki/tls/certs/calls-category-api.crt \
TLS_KEY_PATH=/etc/pki/tls/private/calls-category-api.key \
./deployment/deploy.sh
```

Important:

- If you rerun `deployment/deploy.sh`, it regenerates the `systemd` unit and `nginx` config.
- Do not rely on manual edits inside `/etc/systemd/system/calls-category-api.service` or `/etc/nginx/conf.d/calls-category-api.conf` unless you also stop using the script.
- Prefer passing overrides to the script or updating the script defaults.

## 9. Start and enable the service

After the environment file is complete and TLS paths are correct, run deployment again:

```bash
sudo NGINX_SERVER_NAME=calls-category-api.internal.company \
TLS_CERT_PATH=/etc/pki/tls/certs/calls-category-api.crt \
TLS_KEY_PATH=/etc/pki/tls/private/calls-category-api.key \
./deployment/deploy.sh
```

What this second run does:

- refreshes files in `/opt/calls_category_api`
- updates the virtual environment if needed
- reloads `systemd`
- enables and starts `calls-category-api`
- validates `nginx` configuration with `nginx -t`
- enables and reloads `nginx`
- checks `http://127.0.0.1:8000/healthz`

## 10. Validate the deployment

Check service status:

```bash
sudo systemctl status calls-category-api
sudo systemctl status nginx
```

Check recent logs:

```bash
sudo journalctl -u calls-category-api -n 100 --no-pager
sudo tail -n 100 /opt/calls_category_api/logs/calls_category_api.log
```

Check local health endpoint:

```bash
curl http://127.0.0.1:8000/healthz
```

Expected result:

```json
{"status":"ok"}
```

Check HTTPS through `nginx` from Server B itself:

```bash
curl -k https://calls-category-api.internal.company/healthz
```

If the certificate is already trusted by the host, `-k` is not needed.

## 11. Final functional test

Run one real API call against the deployed service.

From a machine that has access to the server and a sample WAV file:

```bash
curl -X POST "https://calls-category-api.internal.company/v1/calls/process" \
  -H "Authorization: Bearer <REAL_BEARER_TOKEN>" \
  -F "file=@wav/8kHz/4min.wav" \
  -F "call_id=prod-smoke-1" \
  -F "include_extras=true"
```

Expected result:

- HTTP `200`
- JSON response with:
  - `call_id`
  - `transcription`
  - `classification`
  - `timings_ms`

## 12. Required production checks before handover

Before declaring the server ready, confirm all of the following:

- `uvicorn` listens only on `127.0.0.1:8000`
- only `nginx` is exposed externally on `443`
- the API is reachable only from approved internal networks
- the environment file contains real secrets and is mode `600`
- `LOG_LEVEL=INFO`
- `LOG_TRANSCRIPTS=false`
- `VERBOSE_AI_LOGS=false`
- `ENABLE_FFMPEG=true` unless you intentionally want to bypass normalization
- `API_BEARER_TOKEN` is long and random
- if `ffmpeg` is installed, confirm it exists and works:

```bash
/usr/bin/ffmpeg -version
```

- outbound network access to Azure Speech and Azure OpenAI works

## 13. Routine update procedure

For future updates:

1. Upload the new project version to the server.
2. Go into the project directory containing the updated code.
3. Run the deployment script again with the same `NGINX_SERVER_NAME` and TLS overrides.

Example:

```bash
cd /home/admin/calls_category_api
sudo NGINX_SERVER_NAME=calls-category-api.internal.company \
TLS_CERT_PATH=/etc/pki/tls/certs/calls-category-api.crt \
TLS_KEY_PATH=/etc/pki/tls/private/calls-category-api.key \
./deployment/deploy.sh
```

The script is designed to be rerun safely for standard updates.

## 14. Common troubleshooting

If `calls-category-api` does not start:

```bash
sudo journalctl -u calls-category-api -n 100 --no-pager
```

Common causes:

- placeholder values still exist in `/etc/calls-category-api/calls-category-api.env`
- uploaded WAV is not directly compatible and `ffmpeg` is missing
- uploaded WAV is not directly compatible and `ENABLE_FFMPEG=false`
- Azure credentials are wrong
- outbound Azure network access is blocked
- a Python dependency failed to install during deployment

If `nginx -t` fails:

- confirm `NGINX_SERVER_NAME` is correct
- confirm certificate and key paths exist
- confirm the key file is readable by `nginx`

If the health check works locally but not from Server A:

- check firewall rules
- check DNS resolution from Server A
- check TLS certificate trust
- confirm Server A sends the Bearer token

## 15. Files created on the server

Final production file layout:

- application code: `/opt/calls_category_api`
- environment file: `/etc/calls-category-api/calls-category-api.env`
- service file: `/etc/systemd/system/calls-category-api.service`
- nginx config: `/etc/nginx/conf.d/calls-category-api.conf`
- application log: `/opt/calls_category_api/logs/calls_category_api.log`

## 16. Recommended handover note

When handing the server over to the business or support team, include:

- API URL
- Bearer token storage owner
- Azure resource owner
- DNS owner
- TLS certificate renewal owner
- server administrator owner
- exact update procedure from Section 12
