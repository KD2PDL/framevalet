#!/usr/bin/env bash
# framevalet Proxmox installer: creates an unprivileged Debian 12 LXC and installs
# framevalet natively under systemd (no Docker layer). Run on a Proxmox VE host:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/KD2PDL/framevalet/main/proxmox/install.sh)"
set -euo pipefail

REPO="https://github.com/KD2PDL/framevalet.git"
HOSTNAME="framevalet"
DISK="4"      # GB
RAM="512"     # MB
CORES="1"
BRIDGE="vmbr0"

command -v pct >/dev/null || { echo "This must run on a Proxmox VE host."; exit 1; }

CTID="${CTID:-$(pvesh get /cluster/nextid)}"
read -r -p "Container ID [$CTID]: " x; CTID="${x:-$CTID}"
read -r -p "Hostname [$HOSTNAME]: " x; HOSTNAME="${x:-$HOSTNAME}"
read -r -p "Bridge [$BRIDGE]: " x; BRIDGE="${x:-$BRIDGE}"
read -r -p "Static IP with CIDR (empty = DHCP): " CIP
NET="name=eth0,bridge=$BRIDGE,ip=${CIP:-dhcp}"
[ -n "$CIP" ] && { read -r -p "Gateway: " GW; NET="$NET,gw=$GW"; }

TMPL=$(pveam available --section system | awk '/debian-12-standard/{print $2}' | sort -V | tail -1)
STORAGE=$(pvesm status -content vztmpl | awk 'NR==2{print $1}')
ROOTFS=$(pvesm status -content rootdir | awk 'NR==2{print $1}')
pveam download "$STORAGE" "$TMPL" 2>/dev/null || true

echo "Creating CT $CTID ($HOSTNAME)..."
pct create "$CTID" "$STORAGE:vztmpl/$TMPL" \
  --hostname "$HOSTNAME" --cores "$CORES" --memory "$RAM" \
  --rootfs "$ROOTFS:$DISK" --net0 "$NET" \
  --unprivileged 1 --features nesting=0 --onboot 1 --start 1

echo "Waiting for network..."
for _ in $(seq 1 30); do
  pct exec "$CTID" -- ping -c1 -W2 deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

echo "Installing framevalet..."
pct exec "$CTID" -- bash -c "
set -e
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip rclone curl ca-certificates >/dev/null
# cloudflared (optional Cloudflare Tunnel, configured in Admin > Remote access)
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared bookworm main' > /etc/apt/sources.list.d/cloudflared.list
apt-get update -qq && apt-get install -y -qq cloudflared >/dev/null
git clone -q $REPO /opt/framevalet
python3 -m venv /opt/framevalet/.venv
/opt/framevalet/.venv/bin/pip install -q /opt/framevalet
mkdir -p /var/lib/framevalet && chmod 700 /var/lib/framevalet   # holds tokens
cat > /etc/systemd/system/framevalet.service <<'UNIT'
[Unit]
Description=framevalet - Samsung Frame TV photo manager
After=network-online.target
Wants=network-online.target

[Service]
Environment=DATA_DIR=/var/lib/framevalet
ExecStart=/opt/framevalet/.venv/bin/framevalet
Restart=on-failure
User=root
WorkingDirectory=/opt/framevalet

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now framevalet
"

IP=$(pct exec "$CTID" -- hostname -I | awk '{print $1}')
echo
echo "framevalet is running: http://$IP:8470"
echo "First visit creates the admin account; then add your TV and pair."
echo "For OneDrive sync: pct exec '$CTID' -- rclone config   (one time), then set"
echo "the rclone remote in Admin > Settings."
echo "For remote access: paste a Cloudflare Tunnel token in Admin > Remote access."
echo "Update later with: pct exec $CTID -- bash -c 'cd /opt/framevalet && git pull && .venv/bin/pip install -q . && systemctl restart framevalet'"
