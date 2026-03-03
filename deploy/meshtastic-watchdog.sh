#!/bin/bash
# Meshtastic RX Health Watchdog
# Sends a beacon every run, checks if we've received anything recently
# Triggers recovery if RX has been stuck > RX_STUCK_MINUTES

RX_STUCK_MINUTES=10
LOG_TAG="meshtastic-watchdog"
BRIDGE_SOCK="/tmp/meshtastic-bridge.sock"
STATE_DIR="/var/lib/meshtastic-watchdog"
RECOVERY_COUNT_FILE="$STATE_DIR/recovery_count"

mkdir -p "$STATE_DIR"

log() { logger -t "$LOG_TAG" "$*"; echo "[$(date '+%H:%M:%S')] $LOG_TAG: $*"; }

# --- Detect node type ---
if lsusb 2>/dev/null | grep -q "34e1:1002"; then
    NODE_TYPE="usb"
    USB_DEV="2-3"
else
    NODE_TYPE="spi"
fi

# --- Send beacon (keeps traffic flowing so RX can be validated) ---
if [ -S "$BRIDGE_SOCK" ]; then
    echo '{"cmd":"send","message":"watchdog-beacon","destination":"0xffffffff"}' \
        | socat -T2 - UNIX-CONNECT:"$BRIDGE_SOCK" >/dev/null 2>&1 \
        && log "Beacon sent (${NODE_TYPE})"
fi

# --- Check service uptime (skip recovery if just started) ---
SERVICE_ACTIVE_SECS=$(systemctl show chirpstack-concentratord \
    --property=ActiveEnterTimestampMonotonic --value 2>/dev/null | awk '{printf "%d", $1/1000000}')
MONO_NOW=$(awk '{printf "%d", $1}' /proc/uptime)
SERVICE_UPTIME=$(( MONO_NOW - SERVICE_ACTIVE_SECS ))

if [ "$SERVICE_UPTIME" -lt $(( RX_STUCK_MINUTES * 60 )) ]; then
    log "Service uptime ${SERVICE_UPTIME}s < threshold, skipping RX check"
    exit 0
fi

# --- Check RX health ---
RX_COUNT=$(journalctl -u meshtastic-bridge --no-pager \
    --since "${RX_STUCK_MINUTES} minutes ago" 2>/dev/null \
    | grep -c "RX #")

if [ "$RX_COUNT" -gt 0 ]; then
    log "RX healthy: ${RX_COUNT} packets in last ${RX_STUCK_MINUTES}min"
    echo 0 > "$RECOVERY_COUNT_FILE"
    exit 0
fi

# --- RX stuck — recover ---
RECOVERY_COUNT=$(cat "$RECOVERY_COUNT_FILE" 2>/dev/null || echo 0)
RECOVERY_COUNT=$(( RECOVERY_COUNT + 1 ))
echo "$RECOVERY_COUNT" > "$RECOVERY_COUNT_FILE"

if [ "$RECOVERY_COUNT" -gt 5 ]; then
    log "ERROR: Recovery attempted $RECOVERY_COUNT times, still stuck — needs physical inspection"
    exit 1
fi

log "WARNING: Zero RX for ${RX_STUCK_MINUTES}min — recovery attempt #${RECOVERY_COUNT} (${NODE_TYPE})"

systemctl stop meshtastic-bridge chirpstack-concentratord
sleep 2

if [ "$NODE_TYPE" = "usb" ]; then
    log "USB power cycle on ${USB_DEV}..."
    echo 0 > /sys/bus/usb/devices/${USB_DEV}/authorized
    sleep 4
    echo 1 > /sys/bus/usb/devices/${USB_DEV}/authorized
    sleep 5
fi

systemctl start chirpstack-concentratord
sleep 8
systemctl start meshtastic-bridge
log "Recovery complete — monitoring..."
