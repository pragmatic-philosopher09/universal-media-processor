#!/usr/bin/env bash
# One-shot installer for a fresh Ubuntu 22.04/24.04 VM (tested target: Oracle Cloud Always Free
# VM.Standard.A1.Flex). Installs Docker, opens ports 80/443, clones the repo and starts the app
# behind Caddy with automatic HTTPS.
#
#   curl -fsSL https://raw.githubusercontent.com/pragmatic-philosopher09/universal-media-processor/main/deploy/oracle/install.sh \
#     | sudo bash -s -- [--domain example.com] [--repo URL] [--branch main]
#
# Without --domain a free <public-ip>.sslip.io hostname is used (no registration needed).
set -euo pipefail

REPO="https://github.com/pragmatic-philosopher09/universal-media-processor.git"
BRANCH="main"
DOMAIN=""
APP_DIR="/opt/media-downloader"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --dir) APP_DIR="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then echo "run as root (sudo)" >&2; exit 1; fi

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

log "Installing prerequisites"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git iptables-persistent >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker"
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
systemctl enable --now docker >/dev/null

log "Opening ports 80/443 (Oracle images ship a restrictive iptables ruleset)"
for port in 80 443; do
  iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null \
    || iptables -I INPUT 5 -p tcp --dport "$port" -m conntrack --ctstate NEW -j ACCEPT
done
iptables -C INPUT -p udp --dport 443 -j ACCEPT 2>/dev/null \
  || iptables -I INPUT 5 -p udp --dport 443 -m conntrack --ctstate NEW -j ACCEPT
netfilter-persistent save >/dev/null 2>&1 || true

PUBLIC_IP="$(curl -fsS -4 https://api.ipify.org || curl -fsS -4 https://ifconfig.me)"
if [[ -z "$DOMAIN" ]]; then
  DOMAIN="${PUBLIC_IP//./-}.sslip.io"
  log "No --domain given; using free hostname $DOMAIN"
fi

log "Fetching the app into $APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch -q origin "$BRANCH" && git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
else
  git clone -q --branch "$BRANCH" "$REPO" "$APP_DIR"
fi
cd "$APP_DIR/deploy/oracle"

if [[ ! -f .env ]]; then
  log "Writing default .env (edit later: $APP_DIR/deploy/oracle/.env)"
  cat > .env <<ENV
# Domain served by Caddy (Let's Encrypt certificate is obtained automatically)
DOMAIN=$DOMAIN

# 4 ARM cores: keep one job at a time and modest presets.
ENHANCEMENT_ENABLED=1
MAX_CONCURRENT_JOBS=1
MAX_JOBS_PER_IP=1
MAX_DURATION_SECONDS=180
MAX_SOURCE_DURATION_SECONDS=1200
X264_PRESET=fast
X264_CRF=19
FFMPEG_INTERP_QUALITY=high
JOB_TTL_MINUTES=120

# Instagram login-walls datacenter IPs: paste a THROWAWAY account's cookies.
# INSTAGRAM_COOKIES=sessionid=...; ds_user_id=...; csrftoken=...
# YouTube/TikTok block cloud IPs; a residential proxy is the only fix:
# PROXY_URL=socks5://user:pass@host:port
ENV
fi
grep -q '^DOMAIN=' .env && sed -i "s|^DOMAIN=.*|DOMAIN=$DOMAIN|" .env || echo "DOMAIN=$DOMAIN" >> .env

log "Building and starting (first build takes a few minutes)"
docker compose --env-file .env up -d --build

cat > "$APP_DIR/deploy/oracle/update.sh" <<'UPD'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
git -C ../.. pull -q --ff-only
docker compose --env-file .env up -d --build
docker image prune -f >/dev/null
UPD
chmod +x "$APP_DIR/deploy/oracle/update.sh"

log "Done"
echo "  URL:            https://$DOMAIN   (certificate issues in ~30 s on first visit)"
echo "  Logs:           cd $APP_DIR/deploy/oracle && docker compose logs -f"
echo "  Instagram auth: edit $APP_DIR/deploy/oracle/.env (INSTAGRAM_COOKIES) then: docker compose --env-file .env up -d"
echo "  Update:         $APP_DIR/deploy/oracle/update.sh"
