#!/usr/bin/env bash
# Shared by every script in scripts/. Loads the production-deployment
# connection settings from the repo-root .env and fails loudly if any are
# missing. Sourced, not executed:  source "$(dirname "$0")/_prod-env.sh"
#
# These values are deployment-IDENTIFYING (a server address, login users,
# install paths), so they live in .env — which is gitignored — and are
# deliberately NOT added to .env.example. .env.example documents only the
# values a fresh checkout needs; these are specific to one operator's prod
# host and would be noise (or a small information leak) in the template.
#
# Required .env variables (add them to YOUR .env; they are not shipped):
#   CC_PROD_HOST       prod server hostname or IP used for ssh/scp.
#   CC_PROD_SSH_USER   ssh login user on the prod host (e.g. root).
#   CC_PROD_APP_DIR    absolute path to the install on prod
#                      (e.g. /opt/case-calendar). Holds data/, .env,
#                      config.yaml, out/, and the uv venv.
#   CC_PROD_SERVICE    the systemd unit name AND the unix service account
#                      (assumed identical, the common systemd pattern;
#                      e.g. case-calendar). Used for `systemctl <unit>`
#                      and `sudo -u <account>`.
#   CC_PROD_STAGE_DIR  a directory on prod writable by CC_PROD_SSH_USER
#                      without sudo, used to stage scp drops before they're
#                      cp'd into place (e.g. the ssh user's home, /root).
#
# Example .env block (fill in with your own values):
#   CC_PROD_HOST=203.0.113.10
#   CC_PROD_SSH_USER=root
#   CC_PROD_APP_DIR=/opt/case-calendar
#   CC_PROD_SERVICE=case-calendar
#   CC_PROD_STAGE_DIR=/root
#
# Override the .env location with CC_ENV_FILE=/path/to/.env if needed.

# Resolve the repo root from this file's location so the scripts work from
# any working directory, then locate .env.
_pe_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_pe_env_file="${CC_ENV_FILE:-$_pe_root/.env}"

if [ ! -f "$_pe_env_file" ]; then
    echo "error: .env not found at $_pe_env_file" >&2
    exit 1
fi

# Pull a single KEY=VALUE out of .env (last occurrence wins), tolerating an
# optional `export ` prefix and surrounding single/double quotes. We read
# only the keys we need rather than sourcing the whole file — sourcing would
# pull every secret into this shell and could choke on a value bash can't
# evaluate.
_pe_read() {
    local key="$1" line
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$_pe_env_file" | tail -n1)" || true
    line="${line#*=}"
    line="${line%\"}"; line="${line#\"}"
    line="${line%\'}"; line="${line#\'}"
    printf '%s' "$line"
}

CC_PROD_HOST="$(_pe_read CC_PROD_HOST)"
CC_PROD_SSH_USER="$(_pe_read CC_PROD_SSH_USER)"
CC_PROD_APP_DIR="$(_pe_read CC_PROD_APP_DIR)"
CC_PROD_SERVICE="$(_pe_read CC_PROD_SERVICE)"
CC_PROD_STAGE_DIR="$(_pe_read CC_PROD_STAGE_DIR)"

for _pe_v in CC_PROD_HOST CC_PROD_SSH_USER CC_PROD_APP_DIR CC_PROD_SERVICE CC_PROD_STAGE_DIR; do
    if [ -z "${!_pe_v}" ]; then
        echo "error: $_pe_v is not set in $_pe_env_file" >&2
        echo "       see the header of scripts/_prod-env.sh for what it should be." >&2
        exit 1
    fi
done

# The ssh/scp target used throughout the scripts.
CC_PROD_SSH="${CC_PROD_SSH_USER}@${CC_PROD_HOST}"
