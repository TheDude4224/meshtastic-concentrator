# Meshtastic Concentrator — SX1302/1303 Mesh Network

> Turn 100+ FreedomFi gateways into a Meshtastic-compatible AI mesh network using their built-in LoRa concentrator cards.

## Overview

Standard Meshtastic runs on single-channel radios (SX1262/SX1276). FreedomFi gateways ship with **SX1302/1303 concentrator** cards that can receive on **8 channels simultaneously**. This project builds a custom Meshtastic-compatible daemon that leverages that hardware advantage, creating "super nodes" with better range, sensitivity, and throughput than standard Meshtastic devices.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    OpenClaw Agent (per gateway)              │
│                                                             │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │ Meshtastic  │  │   Other      │  │   AI Model        │  │
│  │ Skill       │  │   Skills     │  │   (Sonnet/etc)    │  │
│  └──────┬──────┘  └──────────────┘  └───────────────────┘  │
│         │                                                    │
│  ┌──────▼──────────────────────────────────────────────┐    │
│  │           Mesh Transport API (abstraction)           │    │
│  │         Unix socket / TCP / MQTT interface           │    │
│  └──────┬─────────────────────────────┬────────────────┘    │
│         │                             │                      │
│  ┌──────▼──────┐              ┌───────▼──────────┐          │
│  │ USB Radio   │              │ Concentrator     │          │
│  │ (prototype) │              │ Daemon (prod)    │          │
│  │ meshtastic- │              │ lora-mesh-daemon │          │
│  │ python      │              │ sx1302_hal       │          │
│  └──────┬──────┘              └───────┬──────────┘          │
│         │                             │                      │
└─────────┼─────────────────────────────┼──────────────────────┘
          │                             │
     ┌────▼────┐                 ┌──────▼──────┐
     │ USB     │                 │ PCIe LoRa   │
     │ Radio   │                 │ SX1302/1303 │
     └────┬────┘                 └──────┬──────┘
          │                             │
          └──────────┬──────────────────┘
                     │ RF (915MHz US)
                     ▼
        ┌────────────────────────┐
        │  Standard Meshtastic   │
        │  Devices (phones,      │
        │  handhelds, sensors)   │
        └────────────────────────┘
```

## Components

### Layer 1 — `lora-mesh-daemon` (src/daemon/)
Low-level LoRa transceiver daemon using Semtech's `sx1302_hal` library.
- Talks directly to the SX1302/1303 hardware via SPI/USB
- Configures radio for Meshtastic-compatible modulation
- Receives on 8 channels simultaneously (concentrator advantage)
- Transmits Meshtastic-compatible LoRa frames
- Exposes raw packet API via Unix socket

**Language:** C or Rust (for direct HAL integration)

### Layer 2 — `meshtastic-compat` (src/protocol/)
Meshtastic protocol implementation.
- Encodes/decodes Meshtastic protobufs (MeshPacket, Data, Position, NodeInfo, etc.)
- Handles mesh routing (rebroadcast, hop limit, flood routing)
- AES256-CTR encryption (compatible with Meshtastic default & custom keys)
- Node ID management and neighbor tracking
- Channel/frequency hopping support (LongFast, MediumSlow, ShortFast, etc.)

**Language:** Python (with protobuf)

### Layer 3 — Mesh Transport Bridge (src/bridge/)
Bridges the concentrator daemon to the protocol layer.
- Connects to `lora-mesh-daemon` for raw packets
- Passes through `meshtastic-compat` for encode/decode
- Exposes high-level API for the OpenClaw skill
- Handles device-to-device mesh message routing
- Optional: bridges to standard Meshtastic MQTT for internet mesh

**Language:** Python

### Layer 4 — OpenClaw Meshtastic Skill (skill/)
OpenClaw skill for AI agents to interact with the mesh.
- Send/receive mesh messages
- List mesh nodes and their status
- Position tracking and distance calculation
- Agent-to-agent communication over mesh (no internet needed)
- Alerting and monitoring capabilities

## Hardware

### FreedomFi Gateway Specs
- **CPU:** x86_64 (Intel Celeron/Atom)
- **RAM:** 4GB DDR4
- **Storage:** 64GB PCIe SSD
- **LoRa:** SX1302/1303 concentrator (PCIe card)
- **Connectivity:** Ethernet, WiFi, USB ports
- **Count available:** 100+

### Supported Meshtastic Radios (prototype phase)
- MeshStick (USB, CH341) — recommended
- RAK WisMesh Pocket (USB)
- Heltec HT-CT62 (USB)
- Any serial-connected Meshtastic device

## Development Phases

### Phase 1 — Prototype (USB Radio + OpenClaw Skill) ← CURRENT
- [ ] OpenClaw Meshtastic skill using meshtastic-python
- [ ] USB Meshtastic radio for testing
- [ ] Basic send/receive/list nodes
- [ ] Transport abstraction layer

### Phase 2 — Concentrator Daemon
- [ ] Identify exact hardware (SX1302 vs 1303, interface type)
- [ ] Build lora-mesh-daemon with sx1302_hal
- [ ] Configure for Meshtastic modulation parameters
- [ ] Test RX on multiple channels
- [ ] Test TX compatibility with standard Meshtastic devices

### Phase 3 — Protocol Compatibility
- [ ] Full Meshtastic protobuf encode/decode
- [ ] Mesh routing (flood, managed)
- [ ] Encryption compatibility
- [ ] Channel preset support (LongFast, etc.)
- [ ] Bridge to standard Meshtastic MQTT

### Phase 4 — Fleet Deployment
- [ ] Base Linux image for FreedomFi gateways
- [ ] Automated provisioning script
- [ ] OpenClaw pre-configured per gateway
- [ ] Meshtastic concentrator daemon auto-start
- [ ] Fleet monitoring dashboard

### Phase 5 — Advanced Features
- [ ] Agent-to-agent mesh communication
- [ ] Offline AI (local models) when internet is down
- [ ] Sensor data ingestion over LoRa
- [ ] CBRS integration (when radios are reflashed)
- [ ] Mesh-based distributed computing

## Meshtastic Protocol Reference

### Modulation Parameters (US915 LongFast)
- Frequency: 906.875 MHz (slot 0) — 914.875 MHz (slot 7)
- Bandwidth: 250 kHz
- Spreading Factor: 11
- Coding Rate: 4/8
- Preamble: 16 symbols
- Sync Word: 0x2B (Meshtastic)

### Packet Structure
```
[Preamble][Sync Word][Header][MeshPacket (protobuf)][CRC]
```

### Encryption
- AES256-CTR
- Default key derived from channel name
- Custom PSK support
- Nonce: packet ID + sender node ID

## File Structure
```
meshtastic-concentrator/
├── README.md                  # This file
├── HARDWARE-RESEARCH.md       # FreedomFi hardware findings
├── docs/
│   ├── ARCHITECTURE.md        # Detailed architecture
│   ├── PROTOCOL.md            # Meshtastic protocol details
│   └── DEPLOYMENT.md          # Fleet deployment guide
├── src/
│   ├── daemon/                # Layer 1: lora-mesh-daemon
│   │   ├── CMakeLists.txt
│   │   ├── main.c
│   │   └── sx1302_config.h
│   ├── protocol/              # Layer 2: meshtastic-compat
│   │   ├── __init__.py
│   │   ├── packets.py
│   │   ├── routing.py
│   │   ├── crypto.py
│   │   └── channels.py
│   └── bridge/                # Layer 3: transport bridge
│       ├── __init__.py
│       ├── bridge.py
│       └── transports/
│           ├── usb.py         # USB Meshtastic (prototype)
│           └── concentrator.py # SX1302 daemon (production)
├── skill/                     # Layer 4: OpenClaw skill
│   ├── SKILL.md
│   ├── meshtastic-tool.py
│   └── requirements.txt
├── configs/
│   ├── gateway-base.yaml      # Base config for FreedomFi
│   ├── meshtastic-us915.yaml  # US915 radio config
│   └── fleet-provision.yaml   # Fleet deployment config
└── tests/
    ├── test_protocol.py
    ├── test_bridge.py
    └── test_skill.py
```

## Links
- [Meshtastic Protocol](https://meshtastic.org/docs/overview/mesh-algo/)
- [Meshtastic Protobufs](https://github.com/meshtastic/protobufs)
- [sx1302_hal](https://github.com/Lora-net/sx1302_hal)
- [meshtastic-python](https://github.com/meshtastic/python)
- [FreedomFi/Nova Labs](https://github.com/magma/magma)

## License
TBD

## Contributors
- Jason (hardware, deployment)
- OryahClaude (architecture, development)
