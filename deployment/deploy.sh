#!/usr/bin/env bash
set -Eeuo pipefail

# One-shot deployment helper for RHEL-like hosts.
# It installs/updates the app, virtual environment, systemd unit, nginx config,
# and validates service health when runtime secrets are configured.

SERVICE_NAME="${SERVICE_NAME:-calls-category-api}"
SERVICE_USER="${SERVICE_USER:-callsapi}"
SERVICE_GROUP="${SERVICE_GROUP:-callsapi}"

INSTALL_DIR="${INSTALL_DIR:-/opt/calls_category_api}"
ENV_DIR="${ENV_DIR:-/etc/calls-category-api}"
ENV_FILE="${ENV_FILE:-${ENV_DIR}/calls-category-api.env}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
APP_PORT="${APP_PORT:-8000}"
UVICORN_WORKERS="${UVICORN_WORKERS:-1}"

ENABLE_NGINX=true
NGINX_SERVER_NAME="${NGINX_SERVER_NAME:-calls-category-api.internal.example.com}"
TLS_CERT_PATH="${TLS_CERT_PATH:-/etc/pki/tls/certs/calls-category-api.crt}"
TLS_KEY_PATH="${TLS_KEY_PATH:-/etc/pki/tls/private/calls-category-api.key}"
NGINX_CLIENT_MAX_BODY_SIZE="${NGINX_CLIENT_MAX_BODY_SIZE:-30m}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SYSTEMD_TARGET="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_TARGET="/etc/nginx/conf.d/${SERVICE_NAME}.conf"
ENV_TEMPLATE="${REPO_ROOT}/deployment/.env.prod.example"

ENV_WAS_CREATED=false

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

usage() {
  cat <<EOF
Usage: sudo ./deployment/deploy.sh [options]

Options:
  --skip-nginx   Deploy app + systemd only (do not configure nginx)
  --help         Show this help message

Environment overrides:
  SERVICE_NAME, SERVICE_USER, SERVICE_GROUP
  INSTALL_DIR, ENV_DIR, ENV_FILE
  PYTHON_BIN, APP_PORT, UVICORN_WORKERS
  NGINX_SERVER_NAME, TLS_CERT_PATH, TLS_KEY_PATH, NGINX_CLIENT_MAX_BODY_SIZE
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-nginx)
      ENABLE_NGINX=false
      shift
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "Run as root (example: sudo ./deployment/deploy.sh)."
  fi
}

require_command() {
  local cmd="$1"
  command -v "${cmd}" >/dev/null 2>&1 || die "Required command not found: ${cmd}"
}

backup_if_exists() {
  local target="$1"
  if [[ -f "${target}" ]]; then
    local ts
    ts="$(date '+%Y%m%d%H%M%S')"
    cp -a "${target}" "${target}.bak.${ts}"
    log "Backup created: ${target}.bak.${ts}"
  fi
}

ensure_service_user() {
  if ! getent group "${SERVICE_GROUP}" >/dev/null 2>&1; then
    log "Creating group: ${SERVICE_GROUP}"
    groupadd --system "${SERVICE_GROUP}"
  fi

  if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    log "Creating user: ${SERVICE_USER}"
    useradd \
      --system \
      --gid "${SERVICE_GROUP}" \
      --home-dir "${INSTALL_DIR}" \
      --shell /sbin/nologin \
      "${SERVICE_USER}"
  fi
}

copy_project_files() {
  log "Copying project files to ${INSTALL_DIR}"
  mkdir -p "${INSTALL_DIR}"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a \
      --exclude '.git' \
      --exclude '.env' \
      --exclude '.venv' \
      --exclude '__pycache__' \
      --exclude '.DS_Store' \
      --exclude 'logs' \
      "${REPO_ROOT}/" "${INSTALL_DIR}/"
  else
    cp -a "${REPO_ROOT}/." "${INSTALL_DIR}/"
    rm -f "${INSTALL_DIR}/.env"
  fi
}

install_python_deps() {
  log "Creating/updating virtual environment"
  "${PYTHON_BIN}" -m venv "${INSTALL_DIR}/.venv"
  "${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip
  "${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
}

prepare_env_file() {
  mkdir -p "${ENV_DIR}"

  if [[ ! -f "${ENV_FILE}" ]]; then
    cp "${ENV_TEMPLATE}" "${ENV_FILE}"
    sed -i.bak \
      -e "s|^TAXONOMY_PATH=.*|TAXONOMY_PATH=${INSTALL_DIR}/categories.yaml|" \
      -e "s|^LOG_FILE=.*|LOG_FILE=${INSTALL_DIR}/logs/calls_category_api.log|" \
      "${ENV_FILE}"
    rm -f "${ENV_FILE}.bak"
    ENV_WAS_CREATED=true
    log "Created env file from template: ${ENV_FILE}"
  else
    log "Using existing env file: ${ENV_FILE}"
  fi

  chmod 600 "${ENV_FILE}"
}

render_systemd_unit() {
  log "Installing systemd unit: ${SYSTEMD_TARGET}"
  backup_if_exists "${SYSTEMD_TARGET}"

  cat > "${SYSTEMD_TARGET}" <<EOF
[Unit]
Description=Call Categorization API (FastAPI/Uvicorn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${INSTALL_DIR}/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port ${APP_PORT} --workers ${UVICORN_WORKERS} --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=5
TimeoutStopSec=60
KillSignal=SIGINT
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=${INSTALL_DIR}/logs
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
}

render_nginx_config() {
  if [[ "${ENABLE_NGINX}" != "true" ]]; then
    log "Skipping nginx config (--skip-nginx)"
    return
  fi

  log "Installing nginx config: ${NGINX_TARGET}"
  backup_if_exists "${NGINX_TARGET}"

  cat > "${NGINX_TARGET}" <<EOF
upstream ${SERVICE_NAME}_upstream {
    server 127.0.0.1:${APP_PORT};
    keepalive 32;
}

server {
    listen 443 ssl http2;
    server_name ${NGINX_SERVER_NAME};

    ssl_certificate ${TLS_CERT_PATH};
    ssl_certificate_key ${TLS_KEY_PATH};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;

    client_max_body_size ${NGINX_CLIENT_MAX_BODY_SIZE};
    client_body_timeout 60s;

    location / {
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Connection "";

        proxy_connect_timeout 10s;
        proxy_send_timeout 3900s;
        proxy_read_timeout 3900s;

        proxy_pass http://${SERVICE_NAME}_upstream;
    }
}
EOF
}

env_has_placeholders() {
  grep -Eq 'replace-me|replace-with-long-random-secret|internal.example.com' "${ENV_FILE}"
}

restart_services() {
  if env_has_placeholders; then
    log "Environment file still has placeholder values; skipping service start."
    log "Edit ${ENV_FILE} and rerun this script."
    return
  fi

  log "Reloading systemd and starting ${SERVICE_NAME}"
  systemctl daemon-reload
  systemctl enable --now "${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}"
  systemctl --no-pager --full status "${SERVICE_NAME}" || true

  if [[ "${ENABLE_NGINX}" == "true" ]]; then
    log "Validating and reloading nginx"
    nginx -t
    systemctl enable --now nginx
    systemctl reload nginx
    systemctl --no-pager --full status nginx || true
  fi
}

check_health() {
  if env_has_placeholders; then
    return
  fi

  local health_url="http://127.0.0.1:${APP_PORT}/healthz"
  log "Checking health endpoint: ${health_url}"
  if curl -fsS --max-time 20 "${health_url}" >/dev/null; then
    log "Health check passed"
  else
    log "Health check failed; recent ${SERVICE_NAME} logs:"
    journalctl -u "${SERVICE_NAME}" -n 80 --no-pager || true
    die "Deployment failed health check"
  fi
}

main() {
  require_root

  require_command "${PYTHON_BIN}"
  require_command systemctl
  require_command curl
  require_command grep
  require_command sed
  if [[ "${ENABLE_NGINX}" == "true" ]]; then
    require_command nginx
  fi

  ensure_service_user
  mkdir -p "${INSTALL_DIR}" "${INSTALL_DIR}/logs" "${ENV_DIR}"

  copy_project_files
  install_python_deps
  prepare_env_file

  chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${INSTALL_DIR}"
  chown root:root "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"

  render_systemd_unit
  render_nginx_config
  restart_services
  check_health

  if [[ "${ENV_WAS_CREATED}" == "true" ]]; then
    log "Environment file created from template: ${ENV_FILE}"
    log "Fill real Azure keys/token, then rerun: sudo ./deployment/deploy.sh"
  fi

  log "Deployment script completed."
}

main
