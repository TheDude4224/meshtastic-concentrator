# meshnode-boat — Marine Navigation Computer
## Vessel: 1988 Carver 3607 | Great Lakes Extended Cruising

**Hardware:**
- Raspberry Pi 4 (4GB+)
- 12" touch screen (flybridge or helm station)
- SenseCAP/RAK IoT HAT (SX1302) — LoRa/Meshtastic
- USB GPS receiver (primary)
- RTL-SDR v4 — AIS receive, NOAA weather radio, spectrum
- Class B AIS Transponder (external) — **REQUIRED before offshore use**

---

## ⚠️ Safety First — Great Lakes Specific

The Great Lakes are NOT inland lakes. Lake Superior can produce 20+ foot seas.
Commercial freighter traffic on all 5 lakes requires AIS TX. **Do not cruise
offshore without a functioning AIS transponder.**

**Minimum safety equipment this system adds:**
- AIS TX — you're visible to 740-foot freighters
- AIS RX — you can see all commercial traffic
- NOAA WX radio — automated weather broadcasts (critical on Superior)
- Full offline chart coverage — no cell signal on Lake Superior
- Position logging and track recording

---

## AIS Transponders — Purchase Links

| Model | Price | Buy | Notes |
|-------|-------|-----|-------|
| **Quark-elec QK-A028B** ⭐ | ~$180 | https://www.quark-elec.com/product/qk-a028b-class-b-ais-transponder/ | Best value, WiFi+USB+NMEA |
| Vesper Cortex M1 | ~$500 | https://vespermarine.com/products/cortex | Premium, best UI, WiFi hotspot |
| Shakespeare AIS-350 | ~$300 | https://www.westmarine.com | Simple, reliable |
| NavNet/Furuno FA-50 | ~$400 | https://www.defender.com | Commercial grade |

**Where to buy:**
- https://www.defender.com — Best US marine electronics dealer
- https://www.westmarine.com — In store + online (Sandusky, Cleveland, Chicago locations near Great Lakes)
- https://www.noonsite.com/buy/ais — Price comparison

> ⚠️ FCC Ship Station License required for AIS TX (free, ~10 min):
> https://wireless.fcc.gov/uls/

---

## Software Stack

| Service | Purpose | Port/Interface |
|---------|---------|----------------|
| `chirpstack-concentratord-sx1302` | LoRa HAT → Meshtastic | ZMQ IPC |
| `meshtastic-bridge` | Mesh protocol bridge | Unix socket |
| `gpsd` | USB GPS daemon | TCP 2947 |
| `rtl_ais` | AIS receive via RTL-SDR v4 | UDP → SignalK |
| `rtl_fm` / `multimon-ng` | NOAA WX radio receive | Audio → alert |
| `SignalK` | Marine instrument hub | HTTP 3000 |
| `OpenCPN` | Chart plotter fullscreen | Display |
| `SoapyRemote` | Remote SDR++ from laptop | TCP 55132 |
| AIS transponder | Class B TX/RX | WiFi/NMEA → SignalK |

---

## Great Lakes Chart Coverage

Download ALL of these in OpenCPN Chart Manager (NOAA ENC — free):
- **Lake Superior** — US_ENCs: US5GL10M, US5GL11M, US5GL12M (get all)
- **Lake Michigan** — US_ENCs: US5GL20M series
- **Lake Huron** — US_ENCs: US5GL30M series + Canadian supplements
- **Lake Erie** — US_ENCs: US5GL40M series
- **Lake Ontario** — US_ENCs: US5GL50M series
- **St. Marys River** (Soo Locks) — critical for Lake Superior access
- **North Channel** (Georgian Bay) — if cruising Canada

**Canadian charts:** Download via CHS (Canadian Hydrographic Service) for Ontario waters.
OpenCPN supports both NOAA ENC and CHS charts simultaneously.

Total download: ~2-4GB — do this at home on good WiFi before any trip.

---

## NOAA Weather Radio (RTL-SDR)

Great Lakes specific WX frequencies:
| Station | Freq | Coverage |
|---------|------|----------|
| WXK-78 | 162.550 | Lake Erie / Cleveland |
| WXM-83 | 162.400 | Lake Michigan / Chicago |
| WWH-89 | 162.425 | Lake Superior / Duluth |
| KEC-83  | 162.475 | Lake Huron |
| KHB-35  | 162.400 | Lake Ontario |

```bash
# Receive and alert on NOAA WX (pipe to speaker or log):
rtl_fm -f 162.550M -M fm -s 22050 | multimon-ng -t raw -a MORSE_CW /dev/stdin
# Or simpler — just pipe to audio out:
rtl_fm -f 162.550M -M fm -s 22050 | aplay -r 22050 -f S16_LE
```

---

## Satellite Comms (Highly Recommended for Lake Superior)

Cell coverage is nonexistent on Lake Superior offshore passages.

| Device | Service | Cost | Notes |
|--------|---------|------|-------|
| **Garmin inReach Mini 2** | Iridium | ~$350 + $15/mo | Two-way text, SOS, tracks to OpenCPN |
| SPOT X | Globalstar | ~$200 + $12/mo | Two-way text, SOS |
| Garmin inReach SE+ | Iridium | ~$500 + $15/mo | Larger screen |

Garmin inReach integrates directly with OpenCPN via plugin — shows your track on the chart and allows route sync.

---

## Power (1988 Carver 3607)

The 3607 has a solid 12V house bank. Recommended wiring:
```
12V house bank
    └── 5A fuse
         └── Pololu D24V50F5 (5V 5A buck converter) ~$25
              └── Pi 4 (USB-C, 5V 3A)
              └── Touch screen (if 5V input)
```
- Do NOT power from the ignition/accessories bus — voltage spikes when engines start can kill the Pi
- Use the house bank with its own dedicated fused circuit
- A **Victron Orion-Tr 12/12-9A isolated DC-DC** is ideal if you want galvanic isolation

---

## Installation

### Step 1 — Flash OpenPlotter
OpenPlotter includes SignalK + OpenCPN pre-configured. Download:
https://openplotter.readthedocs.io/en/latest/getting-started/downloading.html

Flash → set hostname `meshnode-boat`, SSH enabled, user `jason`.

### Step 2 — Concentratord (same as main runbook)
```bash
git clone https://github.com/TheDude4224/meshtastic-concentrator.git ~/meshtastic-concentrator
bash ~/meshtastic-concentrator/deploy/build-concentratord.sh
sudo bash ~/meshtastic-concentrator/deploy/install.sh
```

### Step 3 — GPS
```bash
sudo apt-get install -y gpsd gpsd-clients
# Edit /etc/default/gpsd
# DEVICES="/dev/ttyUSB0"
# GPSD_OPTIONS="-n"
sudo systemctl enable --now gpsd
cgps -s  # verify
```

### Step 4 — RTL-SDR AIS receive
```bash
sudo apt-get install -y rtl-sdr rtl-ais
rtl_test -t  # verify dongle
# AIS → UDP → SignalK:
sudo tee /etc/systemd/system/rtl-ais.service << 'UNIT'
[Unit]
Description=RTL-AIS Receiver
After=network.target

[Service]
ExecStart=/usr/bin/rtl_ais -n -h 127.0.0.1 -P 10110
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl enable --now rtl-ais
```

### Step 5 — SignalK connections
In SignalK dashboard (http://<ip>:3000):
- Add connection: GPSD → localhost:2947
- Add connection: UDP → 0.0.0.0:10110 (AIS from rtl_ais)
- Add connection: AIS transponder per its instructions (USB or WiFi)
- Install plugins: `signalk-to-nmea0183`, `@meri-imperiumi/signalk-weather-alerts`

### Step 6 — OpenCPN
```bash
sudo apt-get install -y opencpn
```
- Download all Great Lakes NOAA ENC charts (Chart Manager → NOAA ENC)
- Enable plugins: AIS display, GRIB weather, tides/currents, anchor watch, Garmin inReach
- Set display: fullscreen, night mode enabled, touch input calibrated

### Step 7 — SoapyRemote (SDR++ from laptop)
```bash
sudo apt-get install -y soapysdr-tools soapysdr-module-rtlsdr
git clone https://github.com/pothosware/SoapyRemote && cd SoapyRemote
mkdir build && cd build && cmake .. && make -j4 && sudo make install
sudo systemctl enable --now soapy-remote  # (create unit as in meshnode-sdr README)
```
Connect SDR++ on your laptop: Source → SoapyRemote → `meshnode-boat.local:55132`

---

## SignalK Data Flow

```
USB GPS ──────────────→ gpsd ──────────────→ SignalK ──→ OpenCPN
RTL-SDR ──→ rtl_ais ──→ UDP:10110 ─────────→ SignalK ──→ AIS overlay
AIS Transponder (WiFi) ────────────────────→ SignalK ──→ TX + RX
Depth sounder (NMEA 0183) ─────────────────→ SignalK ──→ depth display
Garmin inReach ─────────────────────────────→ OpenCPN plugin
                                                │
                                           Web UI :3000
                                           Position via Meshtastic mesh
```

---

## Meshtastic Integration

With a shore node at your marina/home base, the Meshtastic mesh gives you:
- Position reports to shore when in range
- Text messaging to/from shore contacts
- Mesh extends through any other nodes en route

Shore node coverage on the Great Lakes is limited to ~20-30 miles offshore (line of sight from elevated antenna). A node on a bluff or lighthouse would extend this significantly.
