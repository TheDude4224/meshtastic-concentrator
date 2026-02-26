# FreedomFi Gateway Provisioning Guide

## Hardware

| Component | Spec |
|-----------|------|
| CPU | Intel Celeron J1900 (x86_64, 4-core, 2GHz) |
| RAM | 4GB DDR3L |
| Storage | 64GB eMMC (FORESEE FS10C064G) |
| Network | 4x Gigabit Ethernet |
| LoRa | RAK5146 USB (SX1303) |

## Step 1: Create Bootable Debian USB

1. Download **Debian 12 (bookworm) netinst** amd64 ISO:
   - https://www.debian.org/download

2. Flash to USB drive (≥2GB):
   ```bash
   # Linux/macOS — replace /dev/sdX with your USB device
   sudo dd if=debian-12.*-amd64-netinst.iso of=/dev/sdX bs=4M status=progress
   sync
   ```
   Or use [balenaEtcher](https://etcher.balena.io/) on any OS.

## Step 2: BIOS Settings (J1900)

1. Power on the FreedomFi gateway, press **DEL** or **F2** to enter BIOS
2. Configure:
   - **Boot → Boot Option #1** → USB drive
   - **Advanced → SATA** → Ensure eMMC is visible (usually shows as FORESEE)
   - **Advanced → USB** → Enable all USB ports
   - **Boot → Secure Boot** → Disabled (if present)
   - **Boot → UEFI/Legacy** → UEFI preferred, Legacy fallback OK
3. Save & Exit (F10)

## Step 3: Install Debian

1. Boot from USB, select **Install** (text mode is fine)
2. Configure:
   - **Hostname:** `freedomfi-gw-01` (or your naming scheme)
   - **Domain:** leave blank
   - **Root password:** set a strong one (will be locked down later)
   - **User:** create `jason` during install
   - **Partitioning:** Use entire eMMC disk, all files in one partition, ext4
   - **Software selection:** Only select **SSH server** and **standard system utilities** — deselect everything else (especially desktop)
   - **GRUB:** Install to eMMC (/dev/mmcblk0 or similar)
3. Remove USB, reboot

## Step 4: Initial Access

1. Connect ethernet cable to any of the 4 ports
2. Find the gateway's IP (check your router/DHCP leases, or connect a monitor)
3. SSH in:
   ```bash
   ssh jason@<gateway-ip>
   ```

## Step 5: Run the Installer

```bash
# Copy the deploy directory to the gateway
scp -r deploy/ jason@<gateway-ip>:~/deploy/

# SSH in and run
ssh jason@<gateway-ip>
sudo bash ~/deploy/install.sh
```

The script is **idempotent** — safe to run again if interrupted.

## Step 6: Configure

### OpenClaw
Edit `/home/jason/.openclaw/config.json` and replace placeholders:
- `{{GATEWAY_NAME}}` — e.g., `freedomfi-gw-01`
- `{{TELEGRAM_BOT_TOKEN}}` — your bot token from @BotFather
- `{{TELEGRAM_USER_ID}}` — your Telegram numeric user ID

### SSH Keys
```bash
# From your workstation:
ssh-copy-id jason@<gateway-ip>
```

### RAK5146 USB
Plug in the RAK5146 USB concentrator. Verify it's detected:
```bash
lsusb | grep -i ftdi    # Should show FTDI device
ls /dev/ttyUSB*          # Should show /dev/ttyUSB0
```

If the device path differs from `/dev/ttyUSB0`, update `/etc/chirpstack-concentratord/concentratord.toml`.

### Meshtasticd
Edit `/etc/meshtasticd/config.yaml` per your Meshtastic network config.

## Step 7: Start & Verify

```bash
# Start all services
sudo systemctl start chirpstack-concentratord
sudo systemctl start meshtasticd
sudo systemctl start openclaw-gateway

# Check status
sudo systemctl status chirpstack-concentratord
sudo systemctl status meshtasticd
sudo systemctl status openclaw-gateway

# Check logs
journalctl -u chirpstack-concentratord -f
journalctl -u meshtasticd -f
journalctl -u openclaw-gateway -f
```

### Verify Checklist

- [ ] All 4 ethernet ports get DHCP addresses: `ip addr`
- [ ] SSH works with key only (password rejected)
- [ ] RAK5146 detected: `lsusb | grep -i ftdi`
- [ ] Concentratord running without errors
- [ ] meshtasticd running without errors
- [ ] OpenClaw gateway responds to Telegram commands
- [ ] Timezone correct: `timedatectl` shows America/Detroit
- [ ] Auto-updates enabled: `systemctl status unattended-upgrades`

## Troubleshooting

**No `/dev/ttyUSB0`:** Check USB connection, try different port. Run `dmesg | tail -20` after plugging in.

**Concentratord fails:** Check `journalctl -u chirpstack-concentratord`. Common issue: wrong `com_path` in config.

**Network issues:** `networkctl status` shows all interfaces. Restart with `systemctl restart systemd-networkd`.

**Re-run installer:** Safe to do anytime: `sudo bash ~/deploy/install.sh`
