#!/usr/bin/env bash
#
# install_ubuntu.sh -- one-shot TPilot deployment for Ubuntu 24.04 LTS.
#
# UBUNTU MIGRATION STAGE 1. Replaces the Windows batch-file fleet
# (autostart_tpilot.bat, start_*/stop_*/restart_*.bat) with systemd units.
#
# The script is IDEMPOTENT: re-running it upgrades units and dependencies
# without touching the database, the Telethon session files, or .env.TPilot.
#
# It deliberately does NOT:
#   * create or migrate the database (storage.py applies its own additive
#     migrations at runtime),
#   * write secrets (it only creates a .env.TPilot skeleton if none exists),
#   * start managers (the operator enables the keys they want).
#
# Usage:
#   sudo ./deploy/install_ubuntu.sh                      # install/upgrade
#   sudo ./deploy/install_ubuntu.sh --dry-run            # print, change nothing
#   sudo TPILOT_DIR=/srv/tpilot ./deploy/install_ubuntu.sh

set -Eeuo pipefail

TPILOT_DIR="${TPILOT_DIR:-/opt/tpilot}"
TPILOT_USER="${TPILOT_USER:-tpilot}"
TPILOT_GROUP="${TPILOT_GROUP:-tpilot}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
DRY_RUN=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
UNIT_SRC="${SCRIPT_DIR}/systemd"
UNIT_DST="/etc/systemd/system"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n'    "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n'   "$*" >&2; exit 1; }

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '\033[0;90m  would run:\033[0m %s\n' "$*"
  else
    "$@"
  fi
}

# ---------------------------------------------------------------------------
# 0. Preconditions
# ---------------------------------------------------------------------------
[[ "$DRY_RUN" == "1" || "$(id -u)" == "0" ]] || die "must run as root (use sudo)"
[[ -f "${REPO_DIR}/main.py" ]] || die "main.py not found in ${REPO_DIR}"

# systemd is required to INSTALL, but --dry-run must stay runnable anywhere
# (CI, a container, a dev laptop) -- its whole job is to let the operator review
# the plan before touching a host, so it must not be gated on the host being the
# final target.
if ! command -v systemctl >/dev/null 2>&1; then
  if [[ "$DRY_RUN" == "1" ]]; then
    warn "systemd not present here; unit activation is only PRINTED (dry run)"
  else
    die "systemd is required"
  fi
fi

log "repo:   ${REPO_DIR}"
log "target: ${TPILOT_DIR}"
log "user:   ${TPILOT_USER}"
[[ "$DRY_RUN" == "1" ]] && warn "DRY RUN -- nothing will be changed"

# ---------------------------------------------------------------------------
# 1. System packages
#    tzdata matters: all business logic is Europe/Kyiv via ZoneInfo, which
#    needs the system tz database present.
# ---------------------------------------------------------------------------
log "installing system packages"
run apt-get update -qq
run apt-get install -y --no-install-recommends \
  "${PYTHON_BIN}" "${PYTHON_BIN}-venv" "${PYTHON_BIN}-dev" \
  build-essential ca-certificates curl git sqlite3 tzdata procps

# ---------------------------------------------------------------------------
# 2. Service account. No login shell, no password: these services never need
#    an interactive session, and the account owns the session files and DB.
# ---------------------------------------------------------------------------
if id -u "$TPILOT_USER" >/dev/null 2>&1; then
  log "user ${TPILOT_USER} already exists"
else
  log "creating system user ${TPILOT_USER}"
  run useradd --system --create-home --home-dir "/home/${TPILOT_USER}" \
      --shell /usr/sbin/nologin "$TPILOT_USER"
fi

# ---------------------------------------------------------------------------
# 3. Application tree
# ---------------------------------------------------------------------------
log "syncing application files to ${TPILOT_DIR}"
run mkdir -p "$TPILOT_DIR"

if [[ "$REPO_DIR" != "$TPILOT_DIR" ]]; then
  if command -v rsync >/dev/null 2>&1; then
    # Never overwrite live state: the DB, sessions, logs and secrets are
    # excluded so a re-run is a pure code upgrade.
    run rsync -a --delete \
      --exclude '.git/' --exclude 'venv/' --exclude '__pycache__/' \
      --exclude '*.pyc' --exclude '.env.TPilot' --exclude 'db/' \
      --exclude 'logs/' --exclude 'runtime/' --exclude 'sessions/' \
      --exclude '*.session' --exclude '*.session-journal' \
      --exclude '*.bak_*' \
      "${REPO_DIR}/" "${TPILOT_DIR}/"
  else
    warn "rsync not found; falling back to cp (no --delete semantics)"
    run cp -a "${REPO_DIR}/." "${TPILOT_DIR}/"
  fi
else
  log "repo is already the target directory; skipping sync"
fi

log "creating runtime directories"
run mkdir -p "${TPILOT_DIR}/db" "${TPILOT_DIR}/logs" \
             "${TPILOT_DIR}/runtime/managers" "${TPILOT_DIR}/sessions"

# ---------------------------------------------------------------------------
# 4. Virtualenv + dependencies
# ---------------------------------------------------------------------------
log "building virtualenv"
if [[ ! -x "${TPILOT_DIR}/venv/bin/python" ]]; then
  run "$PYTHON_BIN" -m venv "${TPILOT_DIR}/venv"
else
  log "venv already present; upgrading in place"
fi
run "${TPILOT_DIR}/venv/bin/python" -m pip install --quiet --upgrade pip wheel
run "${TPILOT_DIR}/venv/bin/python" -m pip install --quiet \
    -r "${TPILOT_DIR}/requirements.txt"

# ---------------------------------------------------------------------------
# 5. Secrets skeleton. Never overwrite an existing file.
# ---------------------------------------------------------------------------
ENV_FILE="${TPILOT_DIR}/.env.TPilot"
if [[ -f "$ENV_FILE" ]]; then
  log ".env.TPilot exists; leaving it untouched"
else
  warn "creating .env.TPilot SKELETON -- fill in the real secrets before starting"
  if [[ "$DRY_RUN" != "1" ]]; then
    cat > "$ENV_FILE" <<'ENVEOF'
# TPilot configuration. Fill every value before starting the services.
# systemd reads this file directly (EnvironmentFile=), so use KEY=value with
# NO surrounding quotes and no inline comments on value lines.

# --- controller identity: these two are what make main.py CONTROLLER_MODE ---
PRIMARY=1
MANAGER_KEY=tpilot
MANAGER_NAME=TPilot

# --- Telegram API ---
API_ID=
API_HASH=
# Optional separate onboarding pair (manager auth fix 2026-06-22)
ONBOARD_API_ID=
ONBOARD_API_HASH=

# --- bots ---
PANEL_BOT_TOKEN=
MANAGER_BOT_TOKEN=
PARTNER_BOT_TOKEN=

# --- panel PIN (required for the Stage 6.1 proxy password reveal) ---
PANEL_ADMIN_PASSWORD=
MANAGER_ADMIN_PASSWORD=

# --- paths (Ubuntu layout) ---
TPILOT_DB_PATH=/opt/tpilot/db/data_tpilot.db

# --- proxy provider (Proxy-Seller). Spend stays guarded in code. ---
PROXY_SELLER_API_KEY=
ENVEOF
  fi
fi

# ---------------------------------------------------------------------------
# 6. Ownership and permissions. Secrets and sessions are 0600; the tree is
#    owned by the service account because UMask=0077 in the units assumes it.
# ---------------------------------------------------------------------------
log "applying ownership and permissions"
run chown -R "${TPILOT_USER}:${TPILOT_GROUP}" "$TPILOT_DIR"
run chmod 750 "$TPILOT_DIR"
[[ -f "$ENV_FILE" ]] && run chmod 600 "$ENV_FILE"
run chmod 700 "${TPILOT_DIR}/db" "${TPILOT_DIR}/sessions"

# ---------------------------------------------------------------------------
# 7. systemd units
# ---------------------------------------------------------------------------
log "installing systemd units"
[[ -d "$UNIT_SRC" ]] || die "unit directory not found: ${UNIT_SRC}"

for unit in "${UNIT_SRC}"/*; do
  name="$(basename "$unit")"
  if [[ "$TPILOT_DIR" == "/opt/tpilot" ]]; then
    run install -m 0644 "$unit" "${UNIT_DST}/${name}"
  else
    # Keep units in sync with a non-default install prefix.
    log "rewriting /opt/tpilot -> ${TPILOT_DIR} in ${name}"
    if [[ "$DRY_RUN" == "1" ]]; then
      printf '\033[0;90m  would install (rewritten):\033[0m %s\n' "${UNIT_DST}/${name}"
    else
      sed -e "s#/opt/tpilot#${TPILOT_DIR}#g" \
          -e "s#^User=tpilot#User=${TPILOT_USER}#" \
          -e "s#^Group=tpilot#Group=${TPILOT_GROUP}#" \
          "$unit" > "${UNIT_DST}/${name}"
      chmod 0644 "${UNIT_DST}/${name}"
    fi
  fi
done

run systemctl daemon-reload

log "enabling services (not starting them yet)"
run systemctl enable tpilot.target tpilot-controller.service tpilot-panel.service \
    tpilot-manager-bot.service tpilot-partner-bot.service \
    tpilot-watchdog.service tpilot-health.service

# ---------------------------------------------------------------------------
# 8. Validation
# ---------------------------------------------------------------------------
log "validating the installation"
if [[ "$DRY_RUN" != "1" ]]; then
  ( cd "$TPILOT_DIR" && sudo -u "$TPILOT_USER" ./venv/bin/python -m py_compile \
      main.py panel_bot.py panel_bridge.py storage.py manager_registry.py \
      stats_engine.py partner_stat_bot.py manager_bot.py process_control.py \
      preflight_check.py soft_watchdog_pinger.py health_server.py ) \
    && log "py_compile OK" || die "py_compile FAILED -- not starting anything"

  ( cd "$TPILOT_DIR" && sudo -u "$TPILOT_USER" ./venv/bin/python \
      tools/process_control_selftest.py >/dev/null ) \
    && log "process_control selftest OK" \
    || warn "process_control selftest FAILED -- inspect before starting services"

  run systemd-analyze verify "${UNIT_DST}/tpilot-controller.service" || \
    warn "systemd-analyze reported warnings (often benign)"
fi

# ---------------------------------------------------------------------------
# 9. Next steps
# ---------------------------------------------------------------------------
cat <<EOF

$(log "installation complete")

Next steps:

  1. Fill in the secrets:
       sudoedit ${ENV_FILE}

  2. Start everything:
       sudo systemctl start tpilot.target

  3. Check status and logs:
       systemctl status tpilot-controller tpilot-panel
       journalctl -u tpilot-controller -f

  4. Enable the manager runtimes you want (one templated instance per key):
       sudo systemctl enable --now tpilot-manager@foxy1
       journalctl -u tpilot-manager@foxy1 -f

  5. List the keys the DB considers active:
       cd ${TPILOT_DIR} && sudo -u ${TPILOT_USER} ./venv/bin/python \\
         manager_registry.py --env ${ENV_FILE} list-active-keys

Group operations (these replace the old .bat fleet):
    sudo systemctl start   tpilot.target     # was start_everything.bat
    sudo systemctl stop    tpilot.target     # was stop_everything.bat
    sudo systemctl restart tpilot.target     # was restart_everything.bat

EOF
