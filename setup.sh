#!/usr/bin/env bash
#
# setup.sh — one-shot setup for CyberQwen PAI.
#
# LAYOUT (this script lives in the PARENT dir, code lives in ./PAI):
#   cyberqwen/
#   ├── setup.sh          <- here
#   ├── Modelfile
#   ├── CyberQwen*.gguf
#   ├── requirements.txt
#   ├── env.example
#   └── PAI/              <- the app (agent.py, server.py, frontend/, search_container/, .venv)
#
# Does: python venv + deps, ~/.cyberqwen data dir, .env, Ollama model create
# (ollama create <name> -f Modelfile) + embedder pull, and the 3 Docker sandboxes
# (created with a restart policy so they come back on boot).
# Idempotent — re-run anytime.
#
# Usage:
#   ./setup.sh                        full setup
#   ./setup.sh --with-kali-tools      also apt-install pentest tools in Kali (slow)
#   ./setup.sh --skip-docker | --skip-ollama | --skip-ui
#   CYBERQWEN_MODEL=cyberqwen ./setup.sh   override the model name (default: Cyberqwen)
#
set -euo pipefail

# ── paths ────────────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # cyberqwen/
APP_DIR="${PROJECT_DIR}/PAI"                                  # the code
VENV="${APP_DIR}/.venv"
DATA_DIR="${HOME}/.cyberqwen"
MODELFILE="${PROJECT_DIR}/Modelfile"
REQ="${PROJECT_DIR}/requirements.txt"

# env.example may be named with or without a leading dot
ENV_EXAMPLE="${PROJECT_DIR}/env.example"; [ -f "$ENV_EXAMPLE" ] || ENV_EXAMPLE="${PROJECT_DIR}/.env.example"
ENV_FILE="${APP_DIR}/.env"

# model name — single source of truth; written into .env so runtime matches.
# NOTE: must match `ollama list` EXACTLY (capitalisation included). The model
# is created as "Cyberqwen" (capital C), so that is the default here.
CHAT_MODEL="${CYBERQWEN_MODEL:-Cyberqwen}"
EMBED_MODEL="${CYBERQWEN_EMBED_MODEL:-nomic-embed-text}"

# docker (names must match mcp_client.py; build dirs are inside PAI/)
# --restart unless-stopped => containers auto-start on boot and after crashes,
# unless you manually `docker stop` them.
RESTART_POLICY="--restart unless-stopped"
KALI_CONTAINER="kali-mcp-sandbox"; KALI_BASE_IMAGE="kalilinux/kali-rolling"
KALI_DIR="${APP_DIR}/kali_sandbox"
# If a Dockerfile exists, bake tools into a custom image; else use the base image.
if [ -f "$KALI_DIR/Dockerfile" ]; then KALI_IMAGE="cyberqwen-kali"; else KALI_IMAGE="$KALI_BASE_IMAGE"; fi
SEARCH_CONTAINER="cyberqwen-search"; SEARCH_IMAGE="cyberqwen-search"; SEARCH_DIR="${APP_DIR}/search_container"
UI_CONTAINER="cyberqwen-ui-app";     UI_IMAGE="cyberqwen-ui";        UI_DIR="${APP_DIR}/frontend"; UI_PORT=3000
KALI_RUN_ARGS="$RESTART_POLICY"
SEARCH_RUN_ARGS="$RESTART_POLICY"
UI_RUN_ARGS="$RESTART_POLICY -p ${UI_PORT}:3000"

WITH_KALI_TOOLS=0; SKIP_DOCKER=0; SKIP_OLLAMA=0; SKIP_UI=0
for a in "$@"; do case "$a" in
  --with-kali-tools) WITH_KALI_TOOLS=1;;
  --skip-docker) SKIP_DOCKER=1;;
  --skip-ollama) SKIP_OLLAMA=1;;
  --skip-ui) SKIP_UI=1;;
  -h|--help) grep '^#' "$0" | sed 's/^# \?//'; exit 0;;
  *) echo "unknown arg: $a"; exit 1;;
esac; done

# ── helpers ──────────────────────────────────────────────────────────────
log(){  printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die(){  printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }
have(){ command -v "$1" >/dev/null 2>&1; }
cstate(){ docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo missing; }

set_env_var(){ # file key value  — insert or replace KEY=value
  local f="$1" k="$2" v="$3"; touch "$f"
  if grep -qE "^${k}=" "$f"; then sed -i "s|^${k}=.*|${k}=${v}|" "$f"
  else echo "${k}=${v}" >> "$f"; fi
}

ensure_image(){ # img dir
  local img="$1" dir="$2"
  docker image inspect "$img" >/dev/null 2>&1 && return 0
  if [ -n "$dir" ] && [ -f "$dir/Dockerfile" ]; then
    log "building image '$img' from $dir"; docker build -t "$img" "$dir"
  else warn "image '$img' missing and no Dockerfile at ${dir:-<none>} — skipping"; return 1; fi
}

ensure_running(){ # name image run_args [cmd...]
  local name="$1" image="$2" runargs="$3"; shift 3
  case "$(cstate "$name")" in
    true)    log "'$name' already running";  docker update $RESTART_POLICY "$name" >/dev/null 2>&1 || true;;
    false)   log "starting existing '$name'"; docker update $RESTART_POLICY "$name" >/dev/null 2>&1 || true; docker start "$name" >/dev/null;;
    missing) log "creating '$name'"; docker run -d --name "$name" $runargs "$image" "$@" >/dev/null;;
  esac
}

[ -d "$APP_DIR" ] || die "expected code dir not found: $APP_DIR (is setup.sh in the parent of PAI/?)"

# ── 1. preflight ─────────────────────────────────────────────────────────
log "Preflight"
have python3 || die "python3 not found (need >= 3.10)"
python3 -c 'import sys;exit(0 if sys.version_info[:2]>=(3,10) else 1)' || die "Python >= 3.10 required"
echo "  python $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])') ok"
have docker || warn "docker not found — container setup skipped"
have ollama || warn "ollama not found — install from https://ollama.com then re-run"

# ── 2. venv + deps ───────────────────────────────────────────────────────
log "Python venv + dependencies  (${VENV})"
[ -d "$VENV" ] || python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip
if [ -f "$REQ" ]; then pip install --quiet -r "$REQ"
else warn "requirements.txt missing — installing core set"
     pip install --quiet fastapi "uvicorn[standard]" numpy requests feedparser mcp; fi
echo "  deps installed"

# ── 3. data dir + .env ───────────────────────────────────────────────────
log "Data dir + .env"
mkdir -p "$DATA_DIR"; echo "  $DATA_DIR ready"
if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$ENV_EXAMPLE" ]; then cp "$ENV_EXAMPLE" "$ENV_FILE"; echo "  created $ENV_FILE from template"
  else touch "$ENV_FILE"; fi
fi

# ── 4. ollama: create the model + pull embedder ──────────────────────────
if [ "$SKIP_OLLAMA" = 0 ] && have ollama; then
  log "Ollama model + embedder"
  if ! curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    warn "Ollama not responding on :11434 — run 'ollama serve', then re-run with --skip-docker"
  else
    # embedder
    ollama pull "$EMBED_MODEL" || warn "pull of $EMBED_MODEL failed"
    # chat model: create from Modelfile if not already present (tolerate :latest tag)
    if ollama list | awk '{print $1}' | sed 's/:latest$//' | grep -qx "$CHAT_MODEL"; then
      echo "  model '$CHAT_MODEL' already exists"
    elif [ -f "$MODELFILE" ]; then
      ls "$PROJECT_DIR"/*.gguf >/dev/null 2>&1 || warn "no .gguf beside Modelfile — check the FROM path"
      log "creating model '$CHAT_MODEL' from Modelfile (ollama create)"
      ( cd "$PROJECT_DIR" && ollama create "$CHAT_MODEL" -f "$MODELFILE" ) \
        || warn "ollama create failed — check Modelfile FROM path points at the .gguf"
    else
      warn "no Modelfile at $MODELFILE — cannot create '$CHAT_MODEL'"
    fi
    # keep runtime name in sync with what we created (prevents the 404 mismatch)
    set_env_var "$ENV_FILE" CYBERQWEN_MODEL "$CHAT_MODEL"
    echo "  set CYBERQWEN_MODEL=$CHAT_MODEL in $ENV_FILE"
    # keep llm_config.json's ollama.chat_model in sync too, if present
    LLM_CFG="${APP_DIR}/llm_config.json"
    if [ -f "$LLM_CFG" ] && have python3; then
      python3 - "$LLM_CFG" "$CHAT_MODEL" <<'PYEOF' 2>/dev/null && echo "  synced llm_config.json ollama.chat_model=$CHAT_MODEL" || true
import json, sys
p, name = sys.argv[1], sys.argv[2]
d = json.load(open(p))
if isinstance(d.get("ollama"), dict):
    d["ollama"]["chat_model"] = name
    json.dump(d, open(p, "w"), indent=2)
PYEOF
    fi
  fi
fi

# ── 5. docker sandboxes ──────────────────────────────────────────────────
if [ "$SKIP_DOCKER" = 0 ] && have docker; then
  log "Docker sandboxes (restart policy: unless-stopped -> auto-start on boot)"
  if [ "$KALI_IMAGE" = "cyberqwen-kali" ]; then
    warn "building the Kali toolset image (multi-GB, can take 20-40 min the first time)"
    ensure_image "$KALI_IMAGE" "$KALI_DIR" || { warn "kali image build failed — using base kali-rolling"; KALI_IMAGE="$KALI_BASE_IMAGE"; }
  fi
  # If the sandbox already exists from the OLD base image, note how to switch it.
  if [ "$KALI_IMAGE" = "cyberqwen-kali" ] && [ "$(cstate "$KALI_CONTAINER")" != "missing" ]; then
    cur_img="$(docker inspect -f '{{.Config.Image}}' "$KALI_CONTAINER" 2>/dev/null || echo "")"
    if [ "$cur_img" != "cyberqwen-kali" ]; then
      warn "existing '$KALI_CONTAINER' runs image '$cur_img', not the tool-baked 'cyberqwen-kali'."
      warn "to switch it:  docker rm -f $KALI_CONTAINER  then re-run ./setup.sh"
    fi
  fi
  ensure_running "$KALI_CONTAINER" "$KALI_IMAGE" "$KALI_RUN_ARGS" tail -f /dev/null
  if [ "$WITH_KALI_TOOLS" = 1 ]; then
    log "installing base pentest tools in '$KALI_CONTAINER' (slow)"
    docker exec "$KALI_CONTAINER" bash -lc '
      apt-get update &&
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        nmap netcat-traditional dnsutils whois curl wget iputils-ping \
        gobuster nikto hydra john seclists smbclient' \
      || warn "kali tool install had errors — install individually as needed"
  fi
  if ensure_image "$SEARCH_IMAGE" "$SEARCH_DIR"; then
    ensure_running "$SEARCH_CONTAINER" "$SEARCH_IMAGE" "$SEARCH_RUN_ARGS" tail -f /dev/null
  fi
  if [ "$SKIP_UI" = 0 ] && ensure_image "$UI_IMAGE" "$UI_DIR"; then
    ensure_running "$UI_CONTAINER" "$UI_IMAGE" "$UI_RUN_ARGS"
  fi
  # make sure the docker daemon itself starts on boot (native Linux; harmless elsewhere)
  if have systemctl; then sudo systemctl enable docker >/dev/null 2>&1 || true; fi
  echo; docker ps --format '  {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E "kali-mcp-sandbox|cyberqwen" || true
fi

# ── 6. done ──────────────────────────────────────────────────────────────
log "Setup complete"
cat <<EOF

Start the backend (from the code dir, in the venv):
  cd "$APP_DIR"
  source .venv/bin/activate
  set -a; . ./.env; set +a          # loads CYBERQWEN_MODEL=$CHAT_MODEL etc.
  python3 server.py                 # (or: uvicorn server:app --host 0.0.0.0 --port 8000)

Handy alias (add to ~/.zshrc, then 'source ~/.zshrc'):
  alias Cyberqwen="cd $APP_DIR && source .venv/bin/activate && set -a && source .env && set +a && python3 server.py"

Then:
  UI          -> http://localhost:${UI_PORT}
  Seed RAG    -> python3 ingest_sources.py --kev     (fast)  |  --all  (full backfill)
  Verify name -> ollama list   (must match CYBERQWEN_MODEL in .env)

Containers are set to restart unless-stopped, so they come back automatically
on boot. The backend (server.py) and Ollama are NOT containers — start the
backend with the alias above; Ollama runs as its own service.

Tip: start Burp + its MCP extension BEFORE the backend to get the +24 Burp tools.
EOF
