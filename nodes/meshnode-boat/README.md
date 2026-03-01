# meshnode-boat — Marine Navigation Computer

**Hardware:**
- Raspberry Pi 4 (4GB+)
- 12" touch screen
- SenseCAP/RAK IoT HAT (SX1302) — LoRa/Meshtastic
- USB GPS receiver
- RTL-SDR v4 — AIS receive + spectrum
- Class B AIS Transponder (external, e.g. Quark-elec QK-A028B) — AIS transmit

---

## Software Stack

| Service | Purpose | Port/Interface |
|---------|---------|----------------|
| `chirpstack-concentratord-sx1302` | LoRa HAT → Meshtastic mesh | ZMQ IPC |
| `meshtastic-bridge` | Mesh protocol bridge | Unix socket |
| `gpsd` | USB GPS daemon | TCP 2947 |
| `rtl-ais` | AIS receive via RTL-SDR | UDP → SignalK |
| `SignalK` | Marine instrument hub | HTTP 3000 |
| `OpenCPN` | Chart plotter (fullscreen touch) | Display |
| `SoapyRemote` | Remote SDR++ access | TCP 55132 |
| AIS transponder | Class B AIS TX/RX | NMEA 0183 / WiFi → SignalK |

---

## Recommended AIS Transponders (TX/RX)

| Model | Price | Interface | Notes |
|-------|-------|-----------|-------|
| **Quark-elec QK-A028B** | ~$180 | USB + WiFi + NMEA | Best value, SignalK friendly |
| Vesper Cortex M1 | ~$500 | WiFi | Premium, best UI |
| Milltech dAISy HAT | ~$90 | SPI (Pi HAT) | **RX only** — no TX |

Purchase links:
- Quark-elec QK-A028B: https://www.quark-elec.com/product/qk-a028b-class-b-ais-transponder/
- Vesper Cortex M1: https://vespermarine.com/products/cortex
- West Marine: https://www.westmarine.com (search "Class B AIS")
- Defender: https://www.defender.com (search "AIS transponder")

> ⚠️ RTL-SDR is receive-only. A dedicated Class B transponder is required for AIS TX.
> AIS TX requires FCC Ship Station License (free, apply at fcc.gov).

---

## Installation

### Step 1 — Flash OpenPlotter OS
Download OpenPlotter image (NOT vanilla Pi OS — OpenPlotter includes SignalK + OpenCPN pre-configured):
https://openplotter.readthedocs.io/en/latest/getting-started/downloading.html

Flash with Raspberry Pi Imager → set hostname `meshnode-boat`, SSH on, user `jason`.

### Step 2 — Build & install concentratord
```bash
bash ~/meshtastic-concentrator/deploy/build-concentratord.sh
sudo bash ~/meshtastic-concentrator/deploy/install.sh
```

### Step 3 — GPS
```bash
sudo apt-get install -y gpsd gpsd-clients
# Edit /etc/default/gpsd:
# DEVICES="/dev/ttyUSB0"  (or /dev/ttyACM0 depending on your GPS)
# GPSD_OPTIONS="-n"
sudo systemctl enable --now gpsd
# Test:
cgps -s
```

### Step 4 — RTL-SDR + AIS receive
```bash
sudo apt-get install -y rtl-sdr rtl-ais
# Test RTL-SDR:
rtl_test -t
# Run AIS receiver (outputs NMEA to UDP port 10110 for SignalK):
rtl_ais -n -h 127.0.0.1 -P 10110 &
```

### Step 5 — SoapyRemote (remote SDR++ access)
```bash
sudo apt-get install -y soapysdr-tools libsoapysdr-dev soapysdr-module-rtlsdr
git clone https://github.com/pothosware/SoapyRemote.git
cd SoapyRemote && mkdir build && cd build
cmake .. && make -j4 && sudo make install
# Start server:
SoapySDRServer --bind="0.0.0.0:55132"
# Connect from SDR++: Source → SoapyRemote → <boat-pi-ip>:55132
```

### Step 6 — SignalK
```bash
# If using OpenPlotter, SignalK is pre-installed
# Otherwise:
sudo npm install -g signalk-server
# Configure at http://<ip>:3000
# Add connections:
#   - GPS: gpsd at localhost:2947
#   - AIS RX: UDP at 0.0.0.0:10110
#   - AIS transponder: USB/WiFi per device instructions
```

### Step 7 — OpenCPN
```bash
sudo apt-get install -y opencpn
# Download NOAA ENC charts via OpenCPN chart manager
# Enable plugins: AIS, weather (GRIB), tides, anchor watch
# Set fullscreen on 12" touch display
```

### Step 8 — Touch screen calibration
```bash
sudo apt-get install -y xinput-calibrator
xinput_calibrator
# Add output to /usr/share/X11/xorg.conf.d/99-calibration.conf
```

---

## SignalK Data Flow

```
USB GPS ──→ gpsd ──────────────────→ SignalK
RTL-SDR ──→ rtl_ais ──→ UDP:10110 ──→ SignalK
AIS Transponder (WiFi/USB/NMEA) ────→ SignalK
Depth sounder (NMEA 0183) ──────────→ SignalK
                                        │
                                   OpenCPN (via SignalK plugin)
                                   Web dashboard (http://<ip>:3000)
                                   Meshtastic bridge (position sharing)
```

---

## Power (Boat 12V)
- Use a **Pololu 5V 5A buck converter** or **Victron Orion** — NOT a car charger
- Pi 4 needs stable 5V 3A minimum
- Add a **fused spur** from the 12V bus (3A fuse)
- Screen may need separate 12V input — check spec

---

## FCC Licensing
AIS TX requires a Ship Station License (free):
https://www.fcc.gov/consumers/guides/ship-station-license
Apply online via ULS: https://wireless.fcc.gov/uls/
