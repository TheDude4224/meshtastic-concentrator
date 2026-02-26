# Architecture — Meshtastic Concentrator

## Why SX1302/1303 Concentrators as Mesh Nodes?

### Standard Meshtastic Node
- Single-channel RX (hears one frequency/SF at a time)
- ~1-5km range typical
- ESP32/nRF52 MCU (limited compute)
- Battery powered, low power

### Our Concentrator Node
- **8-channel simultaneous RX** (hears ALL frequencies/SFs at once)
- Better RX sensitivity = longer range
- Full x86 Linux (4GB RAM, 64GB storage)
- Mains powered (always on)
- OpenClaw AI agent on every node

### The Advantage
A single concentrator node can hear traffic that would require 8+ standard Meshtastic radios to capture. In a mesh network, this means:
- **Near-zero missed packets** — always listening on all channels
- **Better mesh routing** — sees all traffic, makes better forwarding decisions
- **Gateway capability** — bridges mesh traffic to IP networks
- **AI-powered routing** — OpenClaw agent can make intelligent routing decisions

## Transport Abstraction

The key design principle: **the OpenClaw skill doesn't care what radio is underneath.**

```python
class MeshTransport(ABC):
    """Abstract transport — USB radio or concentrator daemon"""

    @abstractmethod
    async def send(self, dest: int, payload: bytes, channel: int = 0) -> bool: ...

    @abstractmethod
    async def receive(self) -> MeshPacket: ...

    @abstractmethod
    async def get_nodes(self) -> list[NodeInfo]: ...

class USBTransport(MeshTransport):
    """Prototype: meshtastic-python over USB serial"""
    ...

class ConcentratorTransport(MeshTransport):
    """Production: custom daemon talking to SX1302/1303"""
    ...
```

## Concentrator Daemon Design

### RX Path (8-channel simultaneous receive)
```
SX1302 Hardware
    ↓ SPI/USB
sx1302_hal (C library)
    ↓ raw LoRa frames
lora-mesh-daemon
    ↓ filter by sync word (0x2B = Meshtastic)
    ↓ Unix socket
meshtastic-compat (Python)
    ↓ protobuf decode
    ↓ decrypt (AES256-CTR)
    ↓ mesh routing logic
OpenClaw Skill
```

### TX Path
```
OpenClaw Skill
    ↓ send command
meshtastic-compat (Python)
    ↓ protobuf encode
    ↓ encrypt (AES256-CTR)
    ↓ Unix socket
lora-mesh-daemon
    ↓ pick TX frequency
sx1302_hal (C library)
    ↓ SPI/USB
SX1302 Hardware → RF
```

### Multi-Channel RX Configuration

The SX1302 has 8 IF (intermediate frequency) channels plus 1 service channel. For Meshtastic US915 LongFast:

| IF Channel | Center Freq (MHz) | Bandwidth | SF Range |
|-----------|-------------------|-----------|----------|
| 0 | 906.875 | 250 kHz | 7-12 |
| 1 | 907.375 | 250 kHz | 7-12 |
| 2 | 907.875 | 250 kHz | 7-12 |
| 3 | 908.375 | 250 kHz | 7-12 |
| 4 | 908.875 | 250 kHz | 7-12 |
| 5 | 909.375 | 250 kHz | 7-12 |
| 6 | 909.875 | 250 kHz | 7-12 |
| 7 | 910.375 | 250 kHz | 7-12 |
| Service | 906.875 | 250 kHz | 11 (LongFast default) |

This means our node simultaneously listens on 8 frequencies while a standard Meshtastic node only listens on 1.

## Mesh Routing Strategy

### Standard Meshtastic Flooding
- Every node rebroadcasts every packet (with hop limit)
- Simple but wasteful
- Works for small networks

### Concentrator-Enhanced Routing
Since our nodes hear ALL traffic:
1. **Intelligent suppression** — Don't rebroadcast if we've already seen the packet forwarded by another node
2. **RSSI-based routing** — Choose best path based on signal strength data from all 8 channels
3. **AI-assisted routing** — OpenClaw agent can learn optimal paths over time
4. **Store and forward** — Full Linux means we can queue messages for offline nodes
5. **Backbone routing** — 100 concentrator nodes form a high-reliability backbone; standard Meshtastic devices are leaf nodes

## Inter-Gateway Communication

Gateways (FreedomFi boxes) can talk to each other via:

1. **LoRa mesh** — Same Meshtastic protocol, gateway-to-gateway
2. **IP network** — If ethernet/WiFi available, direct TCP
3. **MQTT bridge** — Standard Meshtastic MQTT for internet-connected mesh
4. **CBRS LTE** — Future: private LTE between gateways (when Sercomm/BaiCells radios are activated)

## Security Model

### Meshtastic-Compatible
- AES256-CTR encryption (same as standard Meshtastic)
- Channel-based key derivation
- Node authentication via shared PSK

### Enhanced (Gateway-to-Gateway)
- mTLS between concentrator daemons
- Per-gateway certificates
- Encrypted IP backbone between gateways
- OpenClaw session encryption for AI traffic

## Fleet Provisioning

### Base Image Contents
1. Debian minimal (headless)
2. OpenClaw (latest)
3. meshtasticd or lora-mesh-daemon
4. Pre-configured Meshtastic skill
5. Auto-discovery (mDNS) for finding nearby gateways
6. SSH access for management

### Boot Sequence
```
1. BIOS → GRUB → Debian
2. systemd starts:
   a. networking (DHCP or static)
   b. lora-mesh-daemon (or meshtasticd for prototype)
   c. openclaw-gateway
3. OpenClaw bootstraps:
   a. Reads AGENTS.md
   b. Connects to Meshtastic skill
   c. Announces presence on mesh
   d. Ready for commands
```

### Unique Per-Gateway Config
- Node ID (Meshtastic numeric ID)
- Gateway name/label
- GPS coordinates (fixed position)
- OpenClaw agent identity
- Telegram/channel config (optional, for human contact)
