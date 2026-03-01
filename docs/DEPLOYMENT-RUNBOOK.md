# FreedomFi Gateway Deployment Runbook

**Last updated:** 2026-03-01  
**Status:** Battle-tested on first gateway (oryahnoc / 10.0.0.90)

---

## Overview

This runbook covers provisioning a FreedomFi gateway (Intel J1900, RAK5146 USB) from bare OS to
full Meshtastic mesh node with ChirpStack concentratord + meshtastic-bridge.

### Stack

```
RAK5146 USB (/dev/ttyACM0)
    └── chirpstack-concentratord-sx1302   [radio HAL, ZMQ API]
            └── meshtastic-bridge         [mesh protocol, REST-like socket API]
                    └── OpenClaw agent    [NOC brain]
```

---

## Hardware Notes

### RAK5146 USB Variants

| USB ID       | Mode             | Notes |
|--------------|------------------|-------|
| `0483:df11`  | DFU bootloader   | Bricked/stuck — needs firmware flash |
| `34e1:1002`  | USB Virtual COM  | **Normal operation** — shows as `/dev/ttyACM0` |

> ⚠️ If you see `0483:df11`, the card is in DFU mode (likely bricked). Replace the card.  
> Cards that ship NEW will be `34e1:1002` and work immediately.

---

## Step-by-Step Deployment

### Step 1 — OS prerequisites (as root)

```bash
sudo bash deploy/install.sh
```

This installs: build tools, Node.js 22, OpenClaw, Python3, pyzmq, meshtasticd, udev rules, SSH hardening, auto-updates.

### Step 2 — Build concentratord (as deploy user, NOT root)

> **Why build from source?**  
> No working apt package exists. ChirpStack's apt repo doesn't include concentratord for this distro.  
> Must build from source using **brocaar's fork** of sx1302_hal (not the official Lora-net one).

```bash
bash deploy/build-concentratord.sh
```

- Clones `github.com/brocaar/sx1302_hal` @ `V2.1.0r9`  
- Builds and installs HAL to `/usr/local/lib` + `/usr/local/include/libloragw-sx1302/`
- Builds `chirpstack-concentratord-sx1302` v4.6.0
- Installs binary to `/usr/local/bin/chirpstack-concentratord-sx1302`

**Build time:** ~5 min on J1900, ~1 min on modern machine.

> 💡 **Faster builds:** If you have a faster machine (Mac/Linux), run `build-concentratord.sh` there  
> and `scp` the binary to the gateway. The binary is statically linked and portable (x86_64 Linux).
>
> ```bash
> scp /usr/local/bin/chirpstack-concentratord-sx1302 jason@<gateway-ip>:/tmp/
> # Then on gateway:
> sudo mv /tmp/chirpstack-concentratord-sx1302 /usr/local/bin/
> sudo chmod +x /usr/local/bin/chirpstack-concentratord-sx1302
> ```

### Step 3 — Deploy services (as root, after binary is built)

```bash
sudo bash deploy/install.sh --deploy
```

Or manually:
```bash
sudo systemctl start chirpstack-concentratord
sudo systemctl start meshtastic-bridge
sudo systemctl start openclaw-gateway
```

### Step 4 — Verify

```bash
# Check concentratord is running and talking to hardware
sudo systemctl status chirpstack-concentratord
journalctl -u chirpstack-concentratord -f

# Should see:
# INFO Gateway ID retrieved, gateway_id: "XXXXXXXXXXXXXXXX"
# INFO Publishing stats event, rx_received: N, ...

# Check bridge API
echo '{"cmd": "status"}' | socat - UNIX-CONNECT:/tmp/meshtastic-bridge.sock
# Should return: {"status": "ok", "connected": true, ...}
```

---

## Config Files

| File | Purpose |
|------|---------|
| `/etc/chirpstack-concentratord/concentratord.toml` | Radio config: region, channels, model |
| `/etc/meshtastic-bridge/bridge-config.json` | Bridge config: node ID, channel, ZMQ URIs |
| `/home/jason/.openclaw/config.json` | OpenClaw agent config |

### concentratord.toml — Key Fields

```toml
region = "US915"           # Must match your region
model = "rak_5146"         # Note underscore — NOT "rak5146"
model_flags = ["USB"]      # Required for USB variant
```

> ⚠️ Common mistakes:
> - `model = "rak5146"` → panics with "unexpected gateway model"
> - Missing `[gateway.concentrator]` section → all channels freq=0 → FPE crash
> - Wrong com_path → `/dev/ttyUSB0` won't work, must be `/dev/ttyACM0`

---

## Troubleshooting

### concentratord panics: "unexpected gateway model"
```
model = "rak_5146"    ← correct (underscore)
model = "rak5146"     ← WRONG
```

### concentratord FPE crash on start
Missing channel frequencies. Add `[gateway.concentrator]` section with `multi_sf_channels`.

### concentratord: "invalid type: map, expected a string"
Old config format. `[gateway.model]` was a table — it must be `model = "rak_5146"` (string).

### Build fails: `lgw_i2c_set_path` not found
Wrong HAL. Must use `github.com/brocaar/sx1302_hal` @ `V2.1.0r9`, NOT `Lora-net/sx1302_hal`.

### Build fails: `'stddef.h' file not found`
Missing clang deps:
```bash
sudo apt-get install clang libclang-dev
```

### `/dev/ttyACM0` not present
- Run `lsusb` — should see `34e1:1002 RAKwireless`
- If missing: card not plugged in, or card is in DFU mode (`0483:df11` = bricked, replace)
- Check udev: `sudo udevadm control --reload-rules && sudo udevadm trigger`

### rx_count stays 0
- **Check antenna** — must be physically connected to RAK5146
- **Check channel** — other nodes must be on LongFast US915 sub-band 2
- Normal for a new deploy with no nearby nodes

---

## Rollout Plan for Remaining Gateways

1. **Build the binary once on a fast machine** → distribute to all gateways via scp  
   (binary is portable x86_64 Linux, no need to compile on each J1900)

2. **Per gateway:**
   ```bash
   # Push binary
   scp chirpstack-concentratord-sx1302 jason@<ip>:/tmp/
   ssh jason@<ip> "sudo mv /tmp/chirpstack-concentratord-sx1302 /usr/local/bin/ && sudo chmod +x /usr/local/bin/chirpstack-concentratord-sx1302"
   
   # Push repo (if not already there)
   ssh jason@<ip> "git clone https://github.com/TheDude4224/meshtastic-concentrator.git ~/meshtastic-concentrator"
   
   # Run installer
   ssh jason@<ip> "sudo bash ~/meshtastic-concentrator/deploy/install.sh"
   ```

3. **Verify each gateway:**
   ```bash
   ssh jason@<ip> "echo '{\"cmd\": \"status\"}' | socat - UNIX-CONNECT:/tmp/meshtastic-bridge.sock"
   ```

---

## Service Reference

| Service | Command | Notes |
|---------|---------|-------|
| `chirpstack-concentratord` | `systemctl status chirpstack-concentratord` | Radio HAL |
| `meshtastic-bridge` | `systemctl status meshtastic-bridge` | Mesh protocol bridge |
| `openclaw-gateway` | `systemctl status openclaw-gateway` | AI NOC agent |

Bridge API commands (via socat):
```bash
SOCK="UNIX-CONNECT:/tmp/meshtastic-bridge.sock"
echo '{"cmd": "status"}'   | socat - $SOCK
echo '{"cmd": "nodes"}'    | socat - $SOCK
echo '{"cmd": "messages"}' | socat - $SOCK
echo '{"cmd": "my_info"}'  | socat - $SOCK
```

---

## This Gateway (oryahnoc)

- **IP:** 10.0.0.90
- **Hardware:** Intel J1900, 4GB RAM, RAK5146 USB (`34e1:1002`, `/dev/ttyACM0`)
- **Gateway ID:** `0016c001f1531de4`
- **Node ID:** `!19b820d5`
- **Status:** ✅ Fully operational as of 2026-03-01 14:05 EST
