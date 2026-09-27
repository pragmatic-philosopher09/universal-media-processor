# Deploying on Oracle Cloud "Always Free" (or any Ubuntu VPS)

Oracle's Always Free tier gives you a permanent **VM.Standard.A1.Flex** (Ampere ARM) machine with
up to **4 OCPUs and 24 GB RAM** — enough to run the full app *with* 4K 60 fps enhancement, no
spin-down, no monthly hour quota. A card is required for identity verification; Always Free
resources are never billed.

## 1. Create the account and the VM (≈10 minutes, once)

1. Sign up at <https://www.oracle.com/cloud/free/>. Pick a **home region** carefully — Always
   Free ARM capacity is per region and can't be changed later (US/EU regions usually have
   capacity; if "Out of capacity" appears when creating the VM, retry later or script retries).
2. Console → **Compute → Instances → Create instance**:
   - Image: **Ubuntu 24.04** (or 22.04), *aarch64* build.
   - Shape: **Ampere → VM.Standard.A1.Flex**, 4 OCPUs, 24 GB memory (all Always Free).
   - Networking: create a new VCN with a public subnet, **assign a public IPv4 address**.
   - SSH keys: upload your public key (`~/.ssh/id_ed25519.pub`) or generate one.
   - Boot volume: 50–100 GB (200 GB total is free).
3. Open the web ports. Console → **Networking → Virtual cloud networks → your VCN → Security
   Lists → Default Security List → Add Ingress Rules**:
   - Source `0.0.0.0/0`, protocol TCP, destination port **80**
   - Source `0.0.0.0/0`, protocol TCP, destination port **443**
   - (optional, for HTTP/3) Source `0.0.0.0/0`, protocol UDP, destination port **443**

## 2. Install (one command)

```bash
ssh ubuntu@<PUBLIC_IP>
curl -fsSL https://raw.githubusercontent.com/pragmatic-philosopher09/universal-media-processor/main/deploy/oracle/install.sh | sudo bash
```

The script installs Docker, opens the OS firewall, clones this repository into
`/opt/media-downloader`, and starts the app behind **Caddy**, which obtains a Let's Encrypt
certificate automatically. Without a domain it uses a free hostname derived from the IP:
`https://<ip-with-dashes>.sslip.io`.

With your own domain: point an `A` record at the VM's public IP first, then

```bash
curl -fsSL .../install.sh | sudo bash -s -- --domain media.example.com
```

## 3. Instagram (and other) logins

The VM has a datacenter IP, so Instagram will refuse anonymous requests. Put a **throwaway**
account's cookies in `/opt/media-downloader/deploy/oracle/.env`:

```
INSTAGRAM_COOKIES=sessionid=...; ds_user_id=...; csrftoken=...
```

then `cd /opt/media-downloader/deploy/oracle && docker compose --env-file .env up -d`.
YouTube ("confirm you're not a bot") and TikTok (IP-range block) also block cloud IPs; only a
residential proxy (`PROXY_URL=socks5://user:pass@host:port`) gets around that.

## 4. Operations

| Task | Command |
|---|---|
| Logs | `cd /opt/media-downloader/deploy/oracle && docker compose logs -f` |
| Update to latest code | `/opt/media-downloader/deploy/oracle/update.sh` |
| Change settings | edit `deploy/oracle/.env`, then `docker compose --env-file .env up -d` |
| Restart | `docker compose restart` |

Performance on 4 Ampere cores (software x264, `X264_PRESET=fast`): roughly 1× real time for a
1080p30 → 2160p60 enhancement, i.e. a 30-second reel takes about a minute; the script caps
enhancement at 3-minute clips (`MAX_DURATION_SECONDS=180`) and one job at a time.

## Any other VPS

The same `install.sh` works on any Ubuntu 22.04/24.04 host with Docker-capable access (Hetzner,
DigitalOcean, a home server with port forwarding). x86-64 is fine — the Dockerfile handles both
architectures.
