# meshnode-sdr — Meshtastic + Full SDR Node

**Hardware:**
- Raspberry Pi 4
- SenseCAP/RAK IoT HAT (SX1302) — LoRa/Meshtastic
- RTL-SDR v4 — ADS-B, AIS receive, spectrum monitoring

---

## Software Stack

| Service | Purpose | Port |
|---------|---------|------|
| `chirpstack-concentratord-sx1302` | LoRa HAT → Meshtastic | ZMQ IPC |
| `meshtastic-bridge` | Mesh protocol bridge | Unix socket |
| `readsb` | ADS-B decoder (1090 MHz) | 8080 (web) |
| `tar1090` | ADS-B map web UI | 8080 |
| `rtl-ais` | AIS receive (162 MHz) | UDP 10110 |
| `SoapyRemote` | Remote SDR++ access | TCP 55132 |

> Note: readsb, rtl-ais, and SoapyRemote cannot all run simultaneously on one RTL-SDR.
> Default: readsb runs 24/7 for ADS-B. Stop it to use SoapyRemote for SDR++ sessions.
> Adding a second RTL-SDR dongle (~$25) allows 24/7 ADS-B + AIS simultaneously.

---

## Installation

### Concentratord + bridge
Same as main deploy runbook — see `deploy/DEPLOYMENT-RUNBOOK.md`

### ADS-B (readsb + tar1090)
```bash
curl -L https://github.com/wiedehopf/readsb/raw/dev/install.sh | sudo bash
sudo bash -c "$(curl -L https://raw.githubusercontent.com/wiedehopf/tar1090/master/install.sh)"
# Web UI at http://<ip>:8080
```

### SoapyRemote for SDR++
```bash
sudo apt-get install -y soapysdr-tools libsoapysdr-dev soapysdr-module-rtlsdr
git clone https://github.com/pothosware/SoapyRemote.git
cd SoapyRemote && mkdir build && cd build && cmake .. && make -j4 && sudo make install
sudo tee /etc/systemd/system/soapy-remote.service << 'UNIT'
[Unit]
Description=SoapySDR Remote Server
After=network.target

[Service]
ExecStart=/usr/local/bin/SoapySDRServer --bind=0.0.0.0:55132
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl enable --now soapy-remote
```

### SDR++ connection
Source → SoapyRemote → `<node-ip>:55132`
