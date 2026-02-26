#!/usr/bin/env bash
# =============================================================================
# FreedomFi Gateway Provisioning Script
# Target: Debian 12 (bookworm) on Intel Celeron J1900 w/ RAK5146 USB
# Idempotent — safe to run multiple times
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/var/log/freedomfi-install.log"
DEPLOY_USER="jason"
TIMEZONE="America/Detroit"
NODE_MAJOR=22

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

log()  { echo -e "${GREEN}[$(date '+%H:%M:%S')]${NC} $*" | tee -a "$LOG_FILE"; }
warn() { echo -e "${YELLOW}[$(date '+%H:%M:%S')] WARN:${NC} $*" | tee -a "$LOG_FILE"; }
err()  { echo -e "${RED}[$(date '+%H:%M:%S')] ERROR:${NC} $*" | tee -a "$LOG_FILE"; }
die()  { err "$*"; exit 1; }

# Must run as root
[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"

log "=== FreedomFi Gateway Installer Started ==="
log "Logging to $LOG_FILE"

# ---- System basics ----
log "Setting timezone to $TIMEZONE"
timedatectl set-timezone "$TIMEZONE" 2>/dev/null || ln -sf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime

log "Updating package lists..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

log "Installing base packages..."
apt-get install -y -qq \
  build-essential git curl wget gnupg2 apt-transport-https ca-certificates \
  software-properties-common lsb-release usbutils picocom jq \
  python3 python3-pip python3-venv \
  unattended-upgrades apt-listchanges \
  systemd-timesyncd nftables \
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
  log "User created. Add SSH key to /home/$DEPLOY_USER/.ssh/authorized_keys"
fi

# Ensure dialout/plugdev membership (idempotent)
usermod -aG sudo,dialout,plugdev "$DEPLOY_USER" 2>/dev/null || true

# ---- Node.js 22 ----
if command -v node &>/dev/null && node -v | grep -q "^v${NODE_MAJOR}\."; then
  log "Node.js $(node -v) already installed"
else
  log "Installing Node.js $NODE_MAJOR..."
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - >> "$LOG_FILE" 2>&1
  apt-get install -y -qq nodejs >> "$LOG_FILE" 2>&1
  log "Node.js $(node -v) installed"
fi

# ---- OpenClaw ----
if command -v openclaw &>/dev/null; then
  log "OpenClaw already installed: $(openclaw --version 2>/dev/null || echo 'unknown')"
else
  log "Installing OpenClaw globally via npm..."
  npm install -g openclaw >> "$LOG_FILE" 2>&1
  log "OpenClaw installed"
fi

# ---- Python dependencies ----
log "Installing Python packages..."
pip3 install --break-system-packages --quiet \
  meshtastic protobuf pyzmq 2>> "$LOG_FILE" || \
pip3 install --quiet meshtastic protobuf pyzmq 2>> "$LOG_FILE"

# ---- meshtasticd (OpenSUSE Build Service) ----
MESHTASTIC_REPO="/etc/apt/sources.list.d/meshtasticd.list"
if [[ -f "$MESHTASTIC_REPO" ]]; then
  log "meshtasticd repo already configured"
else
  log "Adding meshtasticd repository..."
  MESHTASTIC_KEY="/etc/apt/keyrings/meshtasticd.gpg"
  mkdir -p /etc/apt/keyrings
  curl -fsSL "https://download.opensuse.org/repositories/home:/meshtastic/Debian_12/Release.key" \
    | gpg --dearmor -o "$MESHTASTIC_KEY" 2>/dev/null
  echo "deb [signed-by=$MESHTASTIC_KEY] https://download.opensuse.org/repositories/home:/meshtastic/Debian_12/ /" \
    > "$MESHTASTIC_REPO"
  apt-get update -qq >> "$LOG_FILE" 2>&1
fi

if dpkg -l meshtasticd &>/dev/null; then
  log "meshtasticd already installed"
else
  log "Installing meshtasticd..."
  apt-get install -y -qq meshtasticd >> "$LOG_FILE" 2>&1
fi

# ---- ChirpStack Concentratord ----
CHIRPSTACK_REPO="/etc/apt/sources.list.d/chirpstack.list"
if [[ -f "$CHIRPSTACK_REPO" ]]; then
  log "ChirpStack repo already configured"
else
  log "Adding ChirpStack repository..."
  CHIRPSTACK_KEY="/etc/apt/keyrings/chirpstack.gpg"
  mkdir -p /etc/apt/keyrings
  curl -fsSL "https://artifacts.chirpstack.io/packages/4/deb/key.gpg" \
    | gpg --dearmor -o "$CHIRPSTACK_KEY" 2>/dev/null
  echo "deb [signed-by=$CHIRPSTACK_KEY] https://artifacts.chirpstack.io/packages/4/deb stable main" \
    > "$CHIRPSTACK_REPO"
  apt-get update -qq >> "$LOG_FILE" 2>&1
fi

if dpkg -l chirpstack-concentratord &>/dev/null; then
  log "chirpstack-concentratord already installed"
else
  log "Installing chirpstack-concentratord..."
  apt-get install -y -qq chirpstack-concentratord >> "$LOG_FILE" 2>&1
fi

# Deploy concentratord config
log "Deploying concentratord config..."
mkdir -p /etc/chirpstack-concentratord
cp "$SCRIPT_DIR/concentratord-config.toml" /etc/chirpstack-concentratord/concentratord.toml

# ---- Networking: DHCP on all ethernet ports ----
log "Configuring networking (DHCP on all ethernet interfaces)..."
NETDIR="/etc/systemd/network"
mkdir -p "$NETDIR"

cat > "$NETDIR/20-wired.network" << 'EOF'
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

# Ensure resolv.conf is linked
if [[ ! -L /etc/resolv.conf ]] || [[ "$(readlink /etc/resolv.conf)" != *"systemd"* ]]; then
  ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf 2>/dev/null || true
fi

# ---- SSH Hardening ----
log "Hardening SSH..."
SSHD_CONF="/etc/ssh/sshd_config.d/99-hardened.conf"
mkdir -p /etc/ssh/sshd_config.d
cat > "$SSHD_CONF" << 'EOF'
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
X11Forwarding no
MaxAuthTries 3
AllowUsers jason
EOF

systemctl restart sshd 2>/dev/null || systemctl restart ssh 2>/dev/null || true

# ---- Unattended Upgrades ----
log "Configuring automatic security updates..."
cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

cat > /etc/apt/apt.conf.d/50unattended-upgrades << 'EOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
    "${distro_id}:${distro_codename}";
};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";
EOF

systemctl enable --now unattended-upgrades >> "$LOG_FILE" 2>&1 || true

# ---- Systemd Services ----

# OpenClaw Gateway
log "Setting up openclaw-gateway service..."
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

# meshtasticd service (usually installed by package, but ensure override)
log "Configuring meshtasticd service..."
mkdir -p /etc/systemd/system/meshtasticd.service.d
cat > /etc/systemd/system/meshtasticd.service.d/override.conf << EOF
[Service]
Restart=always
RestartSec=5
EOF

# ChirpStack Concentratord service
log "Setting up chirpstack-concentratord service..."
cat > /etc/systemd/system/chirpstack-concentratord.service << 'EOF'
[Unit]
Description=ChirpStack Concentratord (RAK5146 USB)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/chirpstack-concentratord-sx1302 -c /etc/chirpstack-concentratord/concentratord.toml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Reload and enable services
log "Enabling services..."
systemctl daemon-reload
systemctl enable openclaw-gateway meshtasticd chirpstack-concentratord >> "$LOG_FILE" 2>&1

# Deploy OpenClaw config template if no config exists
OPENCLAW_DIR="/home/$DEPLOY_USER/.openclaw"
if [[ ! -f "$OPENCLAW_DIR/config.json" ]]; then
  log "Deploying OpenClaw config template..."
  mkdir -p "$OPENCLAW_DIR"
  cp "$SCRIPT_DIR/openclaw-config.json.template" "$OPENCLAW_DIR/config.json"
  chown -R "$DEPLOY_USER:$DEPLOY_USER" "$OPENCLAW_DIR"
  warn "Edit /home/$DEPLOY_USER/.openclaw/config.json with your actual values!"
else
  log "OpenClaw config already exists, skipping template"
fi

# ---- USB permissions for RAK5146 ----
log "Setting up udev rules for RAK5146 USB..."
cat > /etc/udev/rules.d/99-rak5146.rules << 'EOF'
# RAK5146 USB LoRa concentrator (SX1303 via USB)
SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="6014", MODE="0666", GROUP="plugdev"
EOF
udevadm control --reload-rules 2>/dev/null || true

# ---- NTP ----
log "Enabling time sync..."
systemctl enable --now systemd-timesyncd >> "$LOG_FILE" 2>&1 || true

# ---- Summary ----
echo ""
echo "============================================="
log "=== Installation Complete ==="
echo "============================================="
echo ""
echo "Installed components:"
echo "  Node.js:         $(node -v 2>/dev/null || echo 'MISSING')"
echo "  npm:             $(npm -v 2>/dev/null || echo 'MISSING')"
echo "  OpenClaw:        $(openclaw --version 2>/dev/null || echo 'installed')"
echo "  Python3:         $(python3 --version 2>/dev/null || echo 'MISSING')"
echo "  meshtasticd:     $(dpkg -l meshtasticd 2>/dev/null | grep -q '^ii' && echo 'installed' || echo 'MISSING')"
echo "  concentratord:   $(dpkg -l chirpstack-concentratord 2>/dev/null | grep -q '^ii' && echo 'installed' || echo 'MISSING')"
echo ""
echo "Services (enable on boot):"
echo "  openclaw-gateway         → systemctl start openclaw-gateway"
echo "  meshtasticd              → systemctl start meshtasticd"
echo "  chirpstack-concentratord → systemctl start chirpstack-concentratord"
echo ""
echo "⚠️  TODO:"
echo "  1. Add SSH public key to /home/$DEPLOY_USER/.ssh/authorized_keys"
echo "  2. Edit /home/$DEPLOY_USER/.openclaw/config.json (fill in placeholders)"
echo "  3. Configure meshtasticd (/etc/meshtasticd/config.yaml)"
echo "  4. Plug in RAK5146 USB concentrator"
echo "  5. Reboot and verify: sudo reboot"
echo ""
log "Full log: $LOG_FILE"
