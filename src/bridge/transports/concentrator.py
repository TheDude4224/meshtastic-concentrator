"""
SX1302/1303 Concentrator Transport — Production implementation.

Talks to the lora-mesh-daemon via Unix socket to send/receive
LoRa packets using the concentrator's multi-channel hardware.

This is a STUB — will be implemented after hardware research
confirms the exact card model and interface.
"""

import asyncio
import json
import logging
from typing import Optional

from .base import (
    MeshTransport, TransportConfig, MeshPacket, NodeInfo, PortNum
)

logger = logging.getLogger(__name__)


class ConcentratorTransport(MeshTransport):
    """
    Transport using the custom lora-mesh-daemon for SX1302/1303 hardware.

    The daemon handles:
    - SPI communication with the concentrator chip
    - 8-channel simultaneous RX
    - TX scheduling
    - Raw LoRa frame encode/decode

    This transport connects to the daemon via Unix socket and
    exchanges JSON-encoded messages.

    Protocol (daemon <-> transport):
    - TX: {"cmd": "send", "freq": 906.875, "sf": 11, "bw": 250000, "payload": "<hex>"}
    - RX: {"event": "rx", "freq": 906.875, "sf": 11, "rssi": -95, "snr": 8.5,
            "if_channel": 3, "payload": "<hex>", "timestamp": 1234567890.123}
    - Status: {"cmd": "status"} -> {"nodes": [...], "rx_count": 1234, "tx_count": 56}
    """

    SOCKET_PATH = "/tmp/lora-mesh-daemon.sock"

    def __init__(self, config: TransportConfig):
        super().__init__(config)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._receive_queue: asyncio.Queue[MeshPacket] = asyncio.Queue()
        self._rx_task: Optional[asyncio.Task] = None
        self._node_db: dict[int, NodeInfo] = {}
        self._my_node_id: int = 0

    async def connect(self) -> bool:
        """Connect to the lora-mesh-daemon Unix socket."""
        socket_path = self.config.device or self.SOCKET_PATH
        try:
            logger.info(f"Connecting to concentrator daemon at {socket_path}")
            self._reader, self._writer = await asyncio.open_unix_connection(socket_path)

            # Request initial status
            await self._send_cmd({"cmd": "status"})
            response = await self._read_response()
            if response and response.get("status") == "ok":
                self._my_node_id = response.get("node_id", 0)
                self._connected = True
                # Start background RX loop
                self._rx_task = asyncio.create_task(self._rx_loop())
                logger.info(f"Connected to concentrator daemon (node {self._my_node_id:#010x})")
                return True
            else:
                logger.error(f"Daemon returned unexpected status: {response}")
                return False

        except FileNotFoundError:
            logger.error(f"Daemon socket not found: {socket_path}")
            logger.error("Is lora-mesh-daemon running? Start it with: sudo lora-mesh-daemon start")
            return False
        except ConnectionRefusedError:
            logger.error(f"Connection refused to daemon at {socket_path}")
            return False
        except Exception as e:
            logger.error(f"Failed to connect to concentrator daemon: {e}")
            return False

    async def disconnect(self) -> None:
        if self._rx_task:
            self._rx_task.cancel()
            try:
                await self._rx_task
            except asyncio.CancelledError:
                pass
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._connected = False
        self._reader = None
        self._writer = None
        logger.info("Disconnected from concentrator daemon")

    async def send_text(self, text: str, destination: int = MeshPacket.BROADCAST,
                        channel: int = 0) -> bool:
        # TODO: Encode as Meshtastic protobuf, encrypt, send via daemon
        payload = text.encode("utf-8")
        return await self.send_raw(destination, payload, PortNum.TEXT_MESSAGE, channel)

    async def send_raw(self, dest: int, payload: bytes, port_num: PortNum,
                       channel: int = 0, want_ack: bool = False) -> bool:
        if not self._connected:
            return False
        try:
            # TODO: Full Meshtastic protobuf encoding + encryption
            cmd = {
                "cmd": "send",
                "dest": dest,
                "port_num": port_num.value,
                "channel": channel,
                "payload": payload.hex(),
                "want_ack": want_ack,
                "hop_limit": self.config.hop_limit,
            }
            await self._send_cmd(cmd)
            response = await self._read_response()
            return response and response.get("status") == "ok"
        except Exception as e:
            logger.error(f"Failed to send via concentrator: {e}")
            return False

    async def get_nodes(self) -> list[NodeInfo]:
        return list(self._node_db.values())

    async def get_my_info(self) -> NodeInfo:
        return NodeInfo(
            node_id=self._my_node_id,
            long_name=f"Concentrator-{self._my_node_id:04x}",
            short_name=f"C{self._my_node_id:03x}",
            is_concentrator=True,
        )

    async def receive(self, timeout: Optional[float] = None) -> Optional[MeshPacket]:
        try:
            return await asyncio.wait_for(
                self._receive_queue.get(),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            return None

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def _send_cmd(self, cmd: dict) -> None:
        """Send a JSON command to the daemon."""
        if self._writer:
            data = json.dumps(cmd).encode() + b"\n"
            self._writer.write(data)
            await self._writer.drain()

    async def _read_response(self, timeout: float = 5.0) -> Optional[dict]:
        """Read a JSON response from the daemon."""
        if not self._reader:
            return None
        try:
            line = await asyncio.wait_for(
                self._reader.readline(),
                timeout=timeout
            )
            if line:
                return json.loads(line.decode())
        except (asyncio.TimeoutError, json.JSONDecodeError) as e:
            logger.error(f"Error reading daemon response: {e}")
        return None

    async def _rx_loop(self) -> None:
        """Background loop reading packets from the daemon."""
        while self._connected and self._reader:
            try:
                line = await self._reader.readline()
                if not line:
                    logger.warning("Daemon connection closed")
                    self._connected = False
                    break

                msg = json.loads(line.decode())
                if msg.get("event") == "rx":
                    packet = self._parse_daemon_packet(msg)
                    if packet:
                        await self._receive_queue.put(packet)
                        await self._notify_callbacks(packet)
                        self._update_node_db(packet)

            except asyncio.CancelledError:
                break
            except json.JSONDecodeError as e:
                logger.warning(f"Invalid JSON from daemon: {e}")
            except Exception as e:
                logger.error(f"Error in RX loop: {e}")
                await asyncio.sleep(1)

    def _parse_daemon_packet(self, msg: dict) -> Optional[MeshPacket]:
        """Parse a daemon RX event into a MeshPacket."""
        try:
            # TODO: Full Meshtastic protobuf decoding + decryption
            return MeshPacket(
                source=msg.get("source", 0),
                destination=msg.get("dest", MeshPacket.BROADCAST),
                packet_id=msg.get("packet_id", 0),
                port_num=PortNum(msg.get("port_num", 1)),
                payload=bytes.fromhex(msg.get("payload", "")),
                channel=msg.get("channel", 0),
                rx_snr=msg.get("snr"),
                rx_rssi=msg.get("rssi"),
                rx_channel=msg.get("if_channel"),  # Which of the 8 IF channels
                rx_time=msg.get("timestamp"),
            )
        except Exception as e:
            logger.error(f"Failed to parse daemon packet: {e}")
            return None

    def _update_node_db(self, packet: MeshPacket) -> None:
        """Update our node database from received packets."""
        if packet.source not in self._node_db:
            self._node_db[packet.source] = NodeInfo(node_id=packet.source)

        node = self._node_db[packet.source]
        node.last_heard = packet.rx_time
        node.snr = packet.rx_snr
        node.rssi = packet.rx_rssi

        if packet.port_num == PortNum.POSITION and packet.position:
            node.latitude = packet.position.get("latitude")
            node.longitude = packet.position.get("longitude")
            node.altitude = packet.position.get("altitude")
