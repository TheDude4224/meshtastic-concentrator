#!/usr/bin/env bash
# =============================================================================
# FreedomFi Gateway Provisioning Script
# Target: Ubuntu 24.04 / Debian 12 on Intel Celeron J1900 w/ RAK5146 USB
# Idempotent — safe to run multiple times
#
# Run order:
#   1. sudo bash install.sh          ← this script (system setup)
#   2. bash build-concentratord.sh   ← build the concentratord binary (run as user)
#   3. sudo bash install.sh --deploy ← deploy services (after binary is built)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/var/log/freedomfi-install.log"
DEPLOY_USER="${SUDO_USER:-jason}"
TIMEZONE="America/Detroit"
NODE_MAJOR=22
DEPLOY_MODE="${1:-}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[$(date '+%H:%M:%S')]${NC} $*" | tee -a "$LOG_FILE"; }
warn() { echo -e "${YELLOW}[$(date '+%H:%M:%S')] WARN:${NC} $*" | tee -a "$LOG_FILE"; }
err()  { echo -e "${RED}[$(date '+%H:%M:%S')] ERROR:${NC} $*" | tee -a "$LOG_FILE"; }
die()  { err "$*"; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"

log "=== FreedomFi Gateway Installer ==="
log "Deploy user: $DEPLOY_USER | Mode: ${DEPLOY_MODE:-full}"

# ---- System basics ----
log "Setting timezone..."
timedatectl set-timezone "$TIMEZONE" 2>/dev/null || ln -sf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime

log "Updating packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

log "Installing base packages..."
apt-get install -y -qq \
  build-essential git curl wget gnupg2 apt-transport-https ca-certificates \
  software-properties-common lsb-release usbutils picocom jq socat \
  python3 python3-pip python3-venv \
  protobuf-compiler libprotobuf-dev clang libclang-dev \
  unattended-upgrades systemd-timesyncd nftables \
  >> "$LOG_FILE" 2>&1

# ---- Create user ----
if id "$DEPLOY_USER" &>/dev/null; then
  log "User '$DEPLOY_USER' already exists"
else
  log "Creating user '$DEPLOY_USER'..."
  useradd -m -s /bin/bash -G sudo,dialout,plugdev "$DEPLOY_USER"
  mkdir -p "/home/$DEPLOY_USER/.ssh"
  chmod 700 "/home/$DEPLOY_USER/.ssh"
  chown -R "$DEPLOY_USER:$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
fi
usermod -aG sudo,dialout,plugdev "$DEPLOY_USER" 2>/dev/null || true

# ---- Node.js ----
if command -v node &>/dev/null && node -v | grep -q "^v${NODE_MAJOR}\."; then
  log "Node.js $(node -v) already installed"
else
  log "Installing Node.js $NODE_MAJOR..."
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - >> "$LOG_FILE" 2>&1
  apt-get install -y -qq nodejs >> "$LOG_FILE" 2>&1
fi

# ---- OpenClaw ----
if command -v openclaw &>/dev/null; then
  log "OpenClaw already installed"
else
  log "Installing OpenClaw..."
  npm install -g openclaw >> "$LOG_FILE" 2>&1
fi

# ---- Python deps ----
log "Installing Python packages..."
pip3 install --break-system-packages --quiet pyzmq protobuf 2>>"$LOG_FILE" || \
pip3 install --quiet pyzmq protobuf 2>>"$LOG_FILE" || true

# ---- meshtastic-bridge Python deps ----
log "Installing meshtastic-bridge deps..."
BRIDGE_REQ="/home/$DEPLOY_USER/meshtastic-concentrator/src/bridge/requirements.txt"
if [[ -f "$BRIDGE_REQ" ]]; then
  pip3 install --break-system-packages --quiet -r "$BRIDGE_REQ" 2>>"$LOG_FILE" || true
fi

# ---- meshtasticd ----
MESHTASTIC_REPO="/etc/apt/sources.list.d/meshtasticd.list"
if [[ ! -f "$MESHTASTIC_REPO" ]]; then
  log "Adding meshtasticd repo..."
  MESHTASTIC_KEY="/etc/apt/keyrings/meshtasticd.gpg"
  mkdir -p /etc/apt/keyrings
  curl -fsSL "https://download.opensuse.org/repositories/home:/meshtastic/Debian_12/Release.key" \
    | gpg --dearmor -o "$MESHTASTIC_KEY" 2>/dev/null
  echo "deb [signed-by=$MESHTASTIC_KEY] https://download.opensuse.org/repositories/home:/meshtastic/Debian_12/ /" \
    > "$MESHTASTIC_REPO"
  apt-get update -qq >> "$LOG_FILE" 2>&1
fi
dpkg -l meshtasticd &>/dev/null || apt-get install -y -qq meshtasticd >> "$LOG_FILE" 2>&1

# ---- udev rules for RAK5146 ----
log "Setting up udev rules..."
cat > /etc/udev/rules.d/99-rak5146.rules << 'EOF'
# RAK5146 USB LoRa concentrator (USB Virtual COM, VID:PID 34e1:1002)
SUBSYSTEM=="tty", ATTRS{idVendor}=="34e1", ATTRS{idProduct}=="1002", SYMLINK+="ttyACM-rak5146", MODE="0666", GROUP="dialout"
# Legacy FT232H variant
SUBSYSTEM=="usb", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6014", MODE="0666", GROUP="plugdev"
EOF
udevadm control --reload-rules 2>/dev/null || true

# ---- Networking ----
log "Configuring networking..."
mkdir -p /etc/systemd/network
cat > /etc/systemd/network/20-wired.network << 'EOF'
[Match]
Type=ether

[Network]
DHCP=yes
LLMNR=yes

[DHCP]
UseDNS=yes
UseNTP=yes
RouteMetric=10
EOF
systemctl enable --now systemd-networkd >> "$LOG_FILE" 2>&1 || true
systemctl enable --now systemd-resolved >> "$LOG_FILE" 2>&1 || true

# ---- SSH Hardening ----
log "Hardening SSH..."
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-hardened.conf << EOF
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
X11Forwarding no
MaxAuthTries 3
AllowUsers $DEPLOY_USER
EOF
systemctl restart sshd 2>/dev/null || systemctl restart ssh 2>/dev/null || true

# ---- Auto-updates ----
log "Configuring unattended-upgrades..."
cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
systemctl enable --now unattended-upgrades >> "$LOG_FILE" 2>&1 || true

# ---- chirpstack-concentratord ----
# NOTE: Not available via apt. Must be built from source using build-concentratord.sh
# Check if binary is already built/installed
CONCENTRATORD_BIN="/usr/local/bin/chirpstack-concentratord-sx1302"
if [[ ! -f "$CONCENTRATORD_BIN" ]]; then
  warn "chirpstack-concentratord binary not found at $CONCENTRATORD_BIN"
  warn "Run as $DEPLOY_USER: bash ${SCRIPT_DIR}/build-concentratord.sh"
  warn "Then re-run: sudo bash $0 --deploy"
else
  log "chirpstack-concentratord binary found: $($CONCENTRATORD_BIN --version 2>/dev/null)"
fi

# Deploy concentratord config
log "Deploying concentratord config..."
mkdir -p /etc/chirpstack-concentratord
cp "$SCRIPT_DIR/concentratord-config.toml" /etc/chirpstack-concentratord/concentratord.toml

# ---- meshtastic-bridge config ----
log "Deploying meshtastic-bridge config..."
mkdir -p /etc/meshtastic-bridge /var/lib/meshtastic-bridge
BRIDGE_CONF="/etc/meshtastic-bridge/bridge-config.json"
if [[ ! -f "$BRIDGE_CONF" ]]; then
  cat > "$BRIDGE_CONF" << 'EOF'
{
  "node_id": null,
  "long_name": "Concentrator Node",
  "short_name": "CNOC",
  "channel": "LongFast",
  "region": "US",
  "concentratord": {
    "event_uri": "ipc:///tmp/concentratord_event",
    "command_uri": "ipc:///tmp/concentratord_command"
  },
  "api": {
    "socket_path": "/tmp/meshtastic-bridge.sock"
  },
  "nodedb_path": "/var/lib/meshtastic-bridge/nodedb.json"
}
EOF
fi
chown -R "$DEPLOY_USER:$DEPLOY_USER" /var/lib/meshtastic-bridge

# ---- Systemd Services ----
log "Installing systemd services..."

cat > /etc/systemd/system/openclaw-gateway.service << EOF
[Unit]
Description=OpenClaw Gateway Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$DEPLOY_USER
WorkingDirectory=/home/$DEPLOY_USER
ExecStart=$(command -v openclaw) gateway start --foreground
Restart=always
RestartSec=10
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/chirpstack-concentratord.service << 'EOF'
[Unit]
Description=ChirpStack Concentratord (RAK5146 USB)
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/chirpstack-concentratord-sx1302 -c /etc/chirpstack-concentratord/concentratord.toml
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

BRIDGE_SRC="/home/$DEPLOY_USER/meshtastic-concentrator/src/bridge/bridge.py"
cat > /etc/systemd/system/meshtastic-bridge.service << EOF
[Unit]
Description=Meshtastic Concentrator Bridge
After=chirpstack-concentratord.service
Requires=chirpstack-concentratord.service

[Service]
Type=simple
User=$DEPLOY_USER
ExecStart=/usr/bin/python3 ${BRIDGE_SRC} -c /etc/meshtastic-bridge/bridge-config.json -v
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

if [[ -f "$CONCENTRATORD_BIN" ]]; then
  log "Enabling and starting all services..."
  systemctl enable --now chirpstack-concentratord meshtastic-bridge openclaw-gateway
else
  log "Enabling non-radio services..."
  systemctl enable openclaw-gateway
  warn "chirpstack-concentratord and meshtastic-bridge NOT started (binary missing)"
fi

# ---- OpenClaw config template ----
OPENCLAW_DIR="/home/$DEPLOY_USER/.openclaw"
if [[ ! -f "$OPENCLAW_DIR/config.json" ]]; then
  mkdir -p "$OPENCLAW_DIR"
  if [[ -f "$SCRIPT_DIR/openclaw-config.json.template" ]]; then
    cp "$SCRIPT_DIR/openclaw-config.json.template" "$OPENCLAW_DIR/config.json"
  fi
  chown -R "$DEPLOY_USER:$DEPLOY_USER" "$OPENCLAW_DIR"
fi

# ---- NTP ----
systemctl enable --now systemd-timesyncd >> "$LOG_FILE" 2>&1 || true

# ---- Summary ----
echo ""
echo "============================================================"
log "=== Installation Complete ==="
echo "============================================================"
echo ""
echo "Component status:"
printf "  %-30s %s\n" "Node.js:" "$(node -v 2>/dev/null || echo 'MISSING')"
printf "  %-30s %s\n" "OpenClaw:" "$(openclaw --version 2>/dev/null || echo 'installed')"
printf "  %-30s %s\n" "Python3:" "$(python3 --version 2>/dev/null || echo 'MISSING')"
printf "  %-30s %s\n" "meshtasticd:" "$(dpkg -l meshtasticd 2>/dev/null | grep -q '^ii' && echo 'installed' || echo 'MISSING')"
printf "  %-30s %s\n" "concentratord binary:" "$([[ -f $CONCENTRATORD_BIN ]] && $CONCENTRATORD_BIN --version 2>/dev/null || echo 'NOT BUILT — run build-concentratord.sh')"
echo ""
echo "⚠️  Manual steps required per gateway:"
echo "  1. Add SSH pubkey: /home/$DEPLOY_USER/.ssh/authorized_keys"
echo "  2. Configure OpenClaw: /home/$DEPLOY_USER/.openclaw/config.json"
echo "  3. If binary not built: run 'bash deploy/build-concentratord.sh' as $DEPLOY_USER"
echo "  4. Plug in RAK5146 USB — verify /dev/ttyACM0 present"
echo "  5. sudo systemctl start chirpstack-concentratord meshtastic-bridge"
echo ""
log "Log: $LOG_FILE"
