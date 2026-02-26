---
name: meshtastic
description: Send and receive messages over Meshtastic mesh radio networks. Use when: (1) sending text messages to mesh nodes, (2) receiving/reading mesh messages, (3) listing known mesh nodes, (4) getting node info (position, battery, SNR, telemetry), (5) any Meshtastic radio interaction. Requires a Meshtastic device connected via USB serial or TCP.
---

# Meshtastic Skill

Interact with Meshtastic mesh radio networks via a JSON API over stdin/stdout.

## Setup

1. Install dependencies: `pip install -r <skill-dir>/requirements.txt`
2. Copy `<skill-dir>/scripts/config.json.template` to `<skill-dir>/scripts/config.json` and edit connection settings
3. Ensure the Meshtastic device is connected (USB or TCP)

## Usage

Run commands via the wrapper script:

```bash
<skill-dir>/scripts/meshtastic_cli.sh <command> [args_json]
```

### Commands

| Command | Args | Description |
|---------|------|-------------|
| `send` | `{"text": "msg", "destination": "!abcd1234"}` | Send text. Omit `destination` for broadcast. |
| `receive` | `{"timeout": 30}` | Listen for messages (default 30s timeout) |
| `nodes` | `{}` | List all known nodes |
| `node_info` | `{"node_id": "!abcd1234"}` | Get detailed node info |
| `my_info` | `{}` | Get local node info |
| `ping` | `{}` | Check device connectivity |

### Response Format

All commands return JSON:

```json
{"ok": true, "data": { ... }}
{"ok": false, "error": "description"}
```

### Examples

```bash
# Send broadcast message
./scripts/meshtastic_cli.sh send '{"text": "Hello mesh!"}'

# Send to specific node
./scripts/meshtastic_cli.sh send '{"text": "Hi", "destination": "!abcd1234"}'

# List nodes
./scripts/meshtastic_cli.sh nodes

# Get node details
./scripts/meshtastic_cli.sh node_info '{"node_id": "!abcd1234"}'

# Listen for messages for 60 seconds
./scripts/meshtastic_cli.sh receive '{"timeout": 60}'
```

## Architecture

The skill uses a transport abstraction layer. The `MeshtasticTransport` interface is implemented by:

- `USBTransport` — talks to meshtastic-python over USB serial (default)
- `TCPTransport` — talks to meshtastic-python over TCP (for remote/networked devices)

To swap to a custom concentrator daemon later, implement a new transport class conforming to the same interface. No changes needed to the CLI or OpenClaw integration.

## Config

Edit `scripts/config.json`:

```json
{
  "transport": "usb",
  "usb": {"device": null},
  "tcp": {"host": "localhost", "port": 4403},
  "receive_timeout": 30,
  "reconnect_attempts": 3,
  "reconnect_delay": 5,
  "log_level": "INFO"
}
```

- `transport`: `"usb"` or `"tcp"`
- `usb.device`: serial device path or `null` for auto-detect
- `tcp.host/port`: TCP connection details
