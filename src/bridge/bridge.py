"""
Concentrator-to-Meshtastic Bridge

Connects ChirpStack Concentratord (ZeroMQ) to our Meshtastic protocol layer,
creating a full Meshtastic-compatible node using the SX1303 concentrator hardware.

Architecture:
    ChirpStack Concentratord (manages RAK5146 hardware)
        ↕ ZeroMQ (raw LoRa frames)
    This Bridge
        ↕ Meshtastic protocol encode/decode + encryption
    OpenClaw Skill API (Unix socket / JSON)

The bridge:
1. Subscribes to Concentratord's ZeroMQ event stream for RX packets
2. Filters for Meshtastic sync word (0x2B)
3. Decrypts and decodes Meshtastic protobufs
4. Handles mesh routing (rebroadcast decisions)
5. Updates the node database
6. Exposes a JSON API for the OpenClaw skill
7. Accepts TX requests, encodes/encrypts, sends via Concentratord
"""

import asyncio
import json
import logging
import signal
import struct
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Meshtastic LoRa sync word
MESHTASTIC_SYNC_WORD = 0x2B


@dataclass
class ConcentratordConfig:
    """Config for connecting to ChirpStack Concentratord."""
    # ZeroMQ endpoints (Concentratord defaults)
    event_url: str = "ipc:///tmp/concentratord_event"
    command_url: str = "ipc:///tmp/concentratord_command"

    # Meshtastic radio settings
    channel_preset: str = "LongFast"
    region: str = "US"
    channel_name: str = "LongFast"
    channel_psk: Optional[str] = None  # hex-encoded PSK, None = default

    # Node identity
    node_id: int = 0  # 0 = auto-generate from MAC
    long_name: str = "Concentrator Node"
    short_name: str = "CONC"

    # Mesh settings
    hop_limit: int = 3
    rebroadcast: bool = True

    # API
    api_socket: str = "/tmp/meshtastic-bridge.sock"
    api_port: int = 0  # TCP port (0 = unix socket only)

    # Paths
    nodedb_path: str = "/var/lib/meshtastic-bridge/nodedb.json"
    state_dir: str = "/var/lib/meshtastic-bridge"


@dataclass
class RxPacket:
    """Raw received packet from Concentratord."""
    payload: bytes
    frequency: int      # Hz
    bandwidth: int      # Hz
    spreading_factor: int
    code_rate: str
    rssi: float
    snr: float
    if_channel: int     # 0-7, which IF channel received it
    timestamp: float    # Unix timestamp
    crc_ok: bool = True


@dataclass
class TxRequest:
    """TX request to send via Concentratord."""
    payload: bytes
    frequency: int      # Hz
    bandwidth: int      # Hz
    spreading_factor: int
    code_rate: str
    tx_power: int       # dBm
    invert_polarity: bool = False
    preamble_length: int = 16


class ConcentratordZMQ:
    """
    Interface to ChirpStack Concentratord via ZeroMQ.

    Concentratord exposes two ZeroMQ sockets:
    - Event socket (SUB): Receives RX packets and gateway stats
    - Command socket (REQ): Sends TX requests and config commands

    Wire format uses Protobuf (chirpstack concentratord API), but we
    handle the serialization here to avoid requiring the full chirpstack
    protobuf package.
    """

    def __init__(self, config: ConcentratordConfig):
        self.config = config
        self._event_socket = None
        self._command_socket = None
        self._context = None
        self._running = False

    async def connect(self) -> bool:
        """Connect to Concentratord ZeroMQ sockets."""
        try:
            import zmq
            import zmq.asyncio

            self._context = zmq.asyncio.Context()

            # Subscribe to all events
            # Sync ZMQ socket — polled via run_in_executor inside receive_event()
            import zmq as _zmq_sync
            self._zmq_sync_ctx = _zmq_sync.Context()
            self._event_socket_sync = self._zmq_sync_ctx.socket(_zmq_sync.SUB)
            self._event_socket_sync.connect(self.config.event_url)
            self._event_socket_sync.subscribe(b"")
            self._event_socket_sync.setsockopt(_zmq_sync.RCVTIMEO, 500)

            # Command socket for TX
            # Sync command socket — avoids asyncio ZMQ deadlock issues
            import zmq as _zmq_sync2
            self._command_socket_sync = self._zmq_sync_ctx.socket(_zmq_sync2.REQ)
            self._command_socket_sync.connect(self.config.command_url)
            self._command_socket_sync.setsockopt(_zmq_sync2.SNDTIMEO, 5000)
            self._command_socket_sync.setsockopt(_zmq_sync2.RCVTIMEO, 5000)

            self._running = True
            logger.info(f"Connected to Concentratord: events={self.config.event_url}, "
                        f"commands={self.config.command_url}")
            return True

        except ImportError:
            logger.error("pyzmq not installed. Run: pip install pyzmq")
            return False
        except Exception as e:
            logger.error(f"Failed to connect to Concentratord: {e}")
            return False

    async def disconnect(self):
        """Disconnect from Concentratord."""
        self._running = False
        if self._event_socket:
            self._event_socket.close()
        if self._command_socket:
            self._command_socket.close()
        if self._context:
            self._context.term()
        logger.info("Disconnected from Concentratord")

    async def receive_event(self) -> Optional[RxPacket]:
        """
        Receive next event from Concentratord.
        Returns RxPacket for uplink frames, None for other events or timeout.
        """
        if not self._event_socket or not self._running:
            return None

        try:
            import asyncio, zmq as _zmq
            loop = asyncio.get_running_loop()
            try:
                data = await loop.run_in_executor(None, self._event_socket_sync.recv)
            except _zmq.Again:
                return None

            # Decode gw.Event { event: oneof { UplinkFrame=1, GatewayStats=2, ... } }
            # Field 1 (UplinkFrame) wire type 2 = 0x0a
            # Field 2 (GatewayStats) wire type 2 = 0x12
            if not data:
                return None

            tag = data[0]
            if tag == 0x0a:
                # UplinkFrame — extract inner bytes (skip tag + varint length)
                pos = 1
                l, s = 0, 0
                while pos < len(data):
                    b = data[pos]; l |= (b & 0x7F) << s; pos += 1
                    if not (b & 0x80): break
                    s += 7
                return self._parse_uplink(data[pos:pos+l])
            elif tag == 0x12:
                logger.debug("Received gateway stats event")
            else:
                logger.debug(f"Unknown event tag: {tag:#04x}")

            return None

        except Exception as e:
            if "Resource temporarily unavailable" not in str(e):
                logger.debug(f"Event receive error: {e}")
            return None

    async def send_downlink(self, tx: TxRequest) -> bool:
        """Send a TX request to Concentratord (legacy, uses TxRequest)."""
        from concentratord_pb import encode_downlink_frame
        from meshtastic_proto import LONGFAST_CONFIG
        downlink_pb = encode_downlink_frame(
            phy_payload=tx.payload,
            frequency=tx.frequency,
            power=tx.tx_power,
            bandwidth=tx.bandwidth,
            spreading_factor=tx.spreading_factor,
            preamble=tx.preamble_length,
        )
        return await self.send_downlink_raw(downlink_pb)

    async def send_downlink_raw(self, command_pb: bytes) -> bool:
        """Send a pre-encoded gw.Command protobuf to Concentratord (single ZMQ frame)."""
        if not self._command_socket or not self._running:
            logger.error("TX: command socket not available")
            return False

        try:
            import asyncio
            loop = asyncio.get_running_loop()

            def _do_tx():
                self._command_socket_sync.send(command_pb)
                return self._command_socket_sync.recv()

            response = await loop.run_in_executor(None, _do_tx)
            logger.debug(f"TX ACK: {response.hex() if response else 'empty (ok)'}")
            return True

        except Exception as e:
            logger.error(f"TX send_downlink_raw failed: {e}")
            return False

    def _parse_uplink(self, data: bytes) -> Optional[RxPacket]:
        """
        Parse a Concentratord uplink event.

        The exact format depends on the Concentratord version.
        We handle both protobuf and simplified formats.
        """
        try:
            # Try to parse as chirpstack gateway protobuf
            # For now, use a simplified parser that extracts the key fields
            # TODO: Full protobuf parsing with chirpstack-api package
            return self._parse_uplink_simplified(data)
        except Exception as e:
            logger.error(f"Failed to parse uplink: {e}")
            return None

    def _parse_uplink_simplified(self, data: bytes) -> Optional[RxPacket]:
        """
        Simplified uplink parser.

        Extracts raw LoRa payload from Concentratord's protobuf output.
        This is a pragmatic approach — we parse just enough of the protobuf
        to extract what we need without requiring the full chirpstack-api package.
        """
        # Protobuf field extraction helpers
        def read_varint(buf, pos):
            result = 0
            shift = 0
            while pos < len(buf):
                b = buf[pos]
                result |= (b & 0x7F) << shift
                pos += 1
                if not (b & 0x80):
                    break
                shift += 7
            return result, pos

        def read_field(buf, pos):
            if pos >= len(buf):
                return None, None, pos
            tag, pos = read_varint(buf, pos)
            field_num = tag >> 3
            wire_type = tag & 0x07

            if wire_type == 0:  # Varint
                value, pos = read_varint(buf, pos)
                return field_num, value, pos
            elif wire_type == 1:  # 64-bit
                value = struct.unpack_from('<q', buf, pos)[0]
                return field_num, value, pos + 8
            elif wire_type == 2:  # Length-delimited
                length, pos = read_varint(buf, pos)
                value = buf[pos:pos + length]
                return field_num, value, pos + length
            elif wire_type == 5:  # 32-bit
                value = struct.unpack_from('<i', buf, pos)[0]
                return field_num, value, pos + 4
            else:
                return None, None, len(buf)  # Skip unknown

        # Extract fields from the protobuf
        payload = None
        rssi = -120.0
        snr = 0.0
        frequency = 906875000
        if_channel = 0

        pos = 0
        while pos < len(data):
            field_num, value, pos = read_field(data, pos)
            if field_num is None:
                break

            # Common protobuf field mappings for gateway uplink frames
            # These vary by Concentratord version; adjust as needed
            if field_num == 1 and isinstance(value, bytes):
                # PHY payload (the raw LoRa frame)
                payload = value
            elif field_num == 11 and isinstance(value, (int, float)):
                rssi = float(value)
            elif field_num == 12 and isinstance(value, (int, float)):
                snr = float(value)
            elif field_num == 3 and isinstance(value, int):
                frequency = value
            elif field_num == 8 and isinstance(value, int):
                if_channel = value

        if payload is None:
            return None

        return RxPacket(
            payload=payload,
            frequency=frequency,
            bandwidth=250000,
            spreading_factor=11,
            code_rate="4/8",
            rssi=rssi,
            snr=snr,
            if_channel=if_channel,
            timestamp=time.time(),
        )

    def _build_tx_command(self, tx: TxRequest) -> bytes:
        """
        Build a TX command for Concentratord.

        Returns protobuf-encoded downlink frame.
        TODO: Use proper chirpstack-api protobuf encoding.
        """
        # Simplified: just the essential fields
        # In production, use the chirpstack-api protobuf definitions
        parts = []

        # Field 1: PHY payload (bytes)
        payload_len = len(tx.payload)
        parts.append(b'\x0a')  # field 1, wire type 2
        parts.append(self._encode_varint(payload_len))
        parts.append(tx.payload)

        # Field 3: frequency (uint32)
        parts.append(b'\x18')  # field 3, wire type 0
        parts.append(self._encode_varint(tx.frequency))

        # Field 5: tx power (int32)
        parts.append(b'\x28')  # field 5, wire type 0
        parts.append(self._encode_varint(tx.tx_power))

        return b''.join(parts)

    @staticmethod
    def _encode_varint(value: int) -> bytes:
        """Encode an integer as a protobuf varint."""
        parts = []
        while value > 0x7F:
            parts.append((value & 0x7F) | 0x80)
            value >>= 7
        parts.append(value & 0x7F)
        return bytes(parts)


class MeshtasticBridge:
    """
    Main bridge connecting Concentratord to Meshtastic protocol.

    Handles:
    - RX: Concentratord → filter → decrypt → decode → nodedb + API
    - TX: API → encode → encrypt → Concentratord
    - Mesh routing decisions
    - Node database management
    - JSON API for OpenClaw skill
    """

    def __init__(self, config: ConcentratordConfig):
        self.config = config
        self.concentratord = ConcentratordZMQ(config)
        self._running = False
        self._api_server = None
        self._rx_count = 0
        self._tx_count = 0
        self._start_time = 0.0

        # These will be initialized from the protocol module
        self._seen_packets: dict[int, float] = {}  # packet_id -> timestamp
        self._node_db: dict[int, dict] = {}
        self._message_log: list[dict] = []
        self._max_message_log = 1000

    async def start(self):
        """Start the bridge."""
        logger.info("Starting Meshtastic Concentrator Bridge")
        self._start_time = time.time()
        self._running = True

        # Ensure state directory exists
        Path(self.config.state_dir).mkdir(parents=True, exist_ok=True)

        # Load node database
        self._load_nodedb()

        # Connect to Concentratord
        if not await self.concentratord.connect():
            logger.error("Failed to connect to Concentratord — is it running?")
            logger.error("Start it with: sudo systemctl start chirpstack-concentratord-sx1302")
            return False

        # Start API server
        await self._start_api_server()

        # Main RX loop
        logger.info(f"Bridge running. API: {self.config.api_socket}")
        logger.info(f"Node ID: {self.config.node_id:#010x} ({self.config.long_name})")

        try:
            await self._rx_loop()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

        return True

    async def stop(self):
        """Stop the bridge."""
        logger.info("Stopping bridge...")
        self._running = False
        self._save_nodedb()
        await self.concentratord.disconnect()
        if self._api_server:
            self._api_server.close()
            await self._api_server.wait_closed()
        logger.info(f"Bridge stopped. RX: {self._rx_count}, TX: {self._tx_count}")

    async def _rx_loop(self):
        """Main receive loop — read from Concentratord, process packets."""
        while self._running:
            try:
                rx = await self.concentratord.receive_event()
                if rx is None:
                    continue

                await self._process_rx(rx)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"RX loop error: {e}")
                await asyncio.sleep(0.1)

    async def _process_rx(self, rx: RxPacket):
        """Process a received LoRa packet."""
        self._rx_count += 1

        # Check if this looks like a Meshtastic packet
        # Meshtastic packets have a specific structure after LoRa demodulation
        if len(rx.payload) < 16:
            logger.debug(f"Packet too short ({len(rx.payload)} bytes), skipping")
            return

        try:
            # Import protocol modules (lazy to avoid circular deps)
            # TODO: Replace with actual protocol module imports once built
            packet_data = {
                "raw": rx.payload.hex(),
                "rssi": rx.rssi,
                "snr": rx.snr,
                "if_channel": rx.if_channel,
                "frequency": rx.frequency,
                "timestamp": rx.timestamp,
                "size": len(rx.payload),
            }

            # Duplicate detection
            # Use first 4 bytes as a simple packet ID for now
            pkt_hash = hash(rx.payload[:16])
            now = time.time()
            if pkt_hash in self._seen_packets:
                if now - self._seen_packets[pkt_hash] < 60:
                    logger.debug(f"Duplicate packet (hash={pkt_hash:#x}), skipping")
                    return
            self._seen_packets[pkt_hash] = now

            # Clean old entries from seen cache
            self._seen_packets = {
                k: v for k, v in self._seen_packets.items()
                if now - v < 300
            }

            # TODO: Full Meshtastic decode once protocol module is ready:
            # 1. Parse header (dest, source, packet_id, flags)
            # 2. Decrypt payload (AES256-CTR)
            # 3. Decode protobuf (MeshPacket → Data → text/position/etc)
            # 4. Update node database
            # 5. Make rebroadcast decision
            # 6. Notify API clients

            logger.info(
                f"RX #{self._rx_count}: {len(rx.payload)}B "
                f"ch={rx.if_channel} rssi={rx.rssi:.0f} snr={rx.snr:.1f} "
                f"freq={rx.frequency/1e6:.3f}MHz"
            )

            # Store in message log
            self._message_log.append(packet_data)
            if len(self._message_log) > self._max_message_log:
                self._message_log = self._message_log[-self._max_message_log:]

        except Exception as e:
            logger.error(f"Error processing RX packet: {e}")

    async def send_text(self, text: str, destination: int = 0xFFFFFFFF,
                        channel: int = 0) -> bool:
        """Send a text message via the mesh."""
        try:
            from meshtastic_proto import build_packet, get_tx_frequency, LONGFAST_CONFIG
            from concentratord_pb import build_command
            import time

            packet_id = int(time.time() * 1000) & 0xFFFFFFFF
            channel_name = self.config.channel_name

            # Build and encrypt the Meshtastic LoRa frame
            phy_payload = build_packet(
                text=text,
                source_id=self.config.node_id,
                dest_id=destination,
                channel_name=channel_name,
                hop_limit=3,
                packet_id=packet_id,
            )

            # Select TX frequency (hop based on packet ID)
            frequency = get_tx_frequency(packet_id)

            logger.info(
                f"TX text to {destination:#010x}: \"{text[:40]}\" "
                f"freq={frequency/1e6:.3f}MHz pkt_id={packet_id:#010x}"
            )

            # Build gw.Command protobuf (single frame for concentratord REP socket)
            command_pb = build_command(
                phy_payload=phy_payload,
                frequency=frequency,
                power=LONGFAST_CONFIG["tx_power"],
                bandwidth=LONGFAST_CONFIG["bandwidth"],
                spreading_factor=LONGFAST_CONFIG["spreading_factor"],
                code_rate=LONGFAST_CONFIG["code_rate"],
                preamble=LONGFAST_CONFIG["preamble_length"],
                downlink_id=packet_id & 0xFFFF,
                gateway_id="",
            )

            # Send to concentratord
            ok = await self.concentratord.send_downlink_raw(command_pb)
            if ok:
                self._tx_count += 1
                logger.info(f"TX #{self._tx_count} sent successfully ({len(phy_payload)}B)")
            else:
                logger.error("TX failed — concentratord rejected downlink")
            return ok

        except ImportError as e:
            logger.error(f"TX requires cryptography package: pip install cryptography")
            logger.error(str(e))
            return False
        except Exception as e:
            logger.error(f"TX failed: {e}", exc_info=True)
            return False

    # ─── API Server ───────────────────────────────────────────────

    async def _start_api_server(self):
        """Start the JSON API server on Unix socket."""
        socket_path = self.config.api_socket

        # Remove stale socket
        Path(socket_path).unlink(missing_ok=True)

        self._api_server = await asyncio.start_unix_server(
            self._handle_api_client,
            path=socket_path,
        )

        # Make socket accessible
        Path(socket_path).chmod(0o666)
        logger.info(f"API server listening on {socket_path}")

    async def _handle_api_client(self, reader: asyncio.StreamReader,
                                  writer: asyncio.StreamWriter):
        """Handle an API client connection."""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                try:
                    request = json.loads(line.decode())
                    response = await self._handle_api_request(request)
                except json.JSONDecodeError:
                    response = {"error": "invalid JSON"}

                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    async def _handle_api_request(self, request: dict) -> dict:
        """Handle a single API request."""
        cmd = request.get("cmd", "")

        if cmd == "send":
            text = request.get("message", request.get("text", ""))
            dest = request.get("destination", 0xFFFFFFFF)
            channel = request.get("channel", 0)
            ok = await self.send_text(text, dest, channel)
            return {"status": "ok" if ok else "error"}

        elif cmd == "nodes":
            return {
                "status": "ok",
                "nodes": list(self._node_db.values()),
                "count": len(self._node_db),
            }

        elif cmd == "status":
            uptime = time.time() - self._start_time
            return {
                "status": "ok",
                "node_id": self.config.node_id,
                "long_name": self.config.long_name,
                "rx_count": self._rx_count,
                "tx_count": self._tx_count,
                "nodes_known": len(self._node_db),
                "uptime_seconds": int(uptime),
                "connected": self.concentratord._running,
                "channel": self.config.channel_preset,
                "region": self.config.region,
            }

        elif cmd == "messages":
            limit = request.get("limit", 50)
            return {
                "status": "ok",
                "messages": self._message_log[-limit:],
                "total": len(self._message_log),
            }

        elif cmd == "my_info":
            return {
                "status": "ok",
                "node_id": self.config.node_id,
                "hex_id": f"!{self.config.node_id:08x}",
                "long_name": self.config.long_name,
                "short_name": self.config.short_name,
                "is_concentrator": True,
                "rx_channels": 8,
                "hardware": "RAK5146/SX1303",
            }

        else:
            return {"error": f"unknown command: {cmd}"}

    # ─── Node Database ────────────────────────────────────────────

    def _load_nodedb(self):
        """Load node database from disk."""
        path = Path(self.config.nodedb_path)
        if path.exists():
            try:
                self._node_db = json.loads(path.read_text())
                logger.info(f"Loaded {len(self._node_db)} nodes from {path}")
            except Exception as e:
                logger.warning(f"Failed to load nodedb: {e}")
                self._node_db = {}

    def _save_nodedb(self):
        """Save node database to disk."""
        path = Path(self.config.nodedb_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._node_db, indent=2))
            logger.info(f"Saved {len(self._node_db)} nodes to {path}")
        except Exception as e:
            logger.warning(f"Failed to save nodedb: {e}")


# ─── CLI Entry Point ──────────────────────────────────────────────

def load_config(path: Optional[str] = None) -> ConcentratordConfig:
    """Load bridge configuration from file or defaults."""
    config = ConcentratordConfig()

    if path:
        config_path = Path(path)
        if config_path.exists():
            data = json.loads(config_path.read_text())
            for key, value in data.items():
                if hasattr(config, key):
                    setattr(config, key, value)
            logger.info(f"Loaded config from {path}")

    # Auto-generate node ID from hostname if not set
    if config.node_id == 0:
        import hashlib
        import socket
        hostname = socket.gethostname()
        config.node_id = int.from_bytes(
            hashlib.sha256(hostname.encode()).digest()[:4],
            "big"
        ) & 0x7FFFFFFF  # Keep positive

    return config


async def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Meshtastic Concentrator Bridge — SX1303 to Meshtastic mesh"
    )
    parser.add_argument("-c", "--config", help="Config file path (JSON)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--event-url", help="Concentratord event ZMQ URL")
    parser.add_argument("--command-url", help="Concentratord command ZMQ URL")
    parser.add_argument("--node-name", help="Node long name")
    parser.add_argument("--api-socket", help="API Unix socket path")

    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Load config
    config = load_config(args.config)
    if args.event_url:
        config.event_url = args.event_url
    if args.command_url:
        config.command_url = args.command_url
    if args.node_name:
        config.long_name = args.node_name
    if args.api_socket:
        config.api_socket = args.api_socket

    # Handle signals
    bridge = MeshtasticBridge(config)
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(bridge.stop()))

    # Run
    await bridge.start()


if __name__ == "__main__":
    asyncio.run(main())
