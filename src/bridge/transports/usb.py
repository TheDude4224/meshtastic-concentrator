"""
USB Meshtastic Transport — Prototype implementation.

Uses meshtastic-python to talk to a USB-connected Meshtastic radio.
This is the prototype transport; production will use ConcentratorTransport.
"""

import asyncio
import logging
from typing import Optional

from .base import (
    MeshTransport, TransportConfig, MeshPacket, NodeInfo, PortNum
)

logger = logging.getLogger(__name__)


class USBTransport(MeshTransport):
    """
    Transport using meshtastic-python library with a USB radio.

    Requires: pip install meshtastic
    Hardware: Any Meshtastic-compatible USB device (MeshStick, T-Beam, etc.)
    """

    def __init__(self, config: TransportConfig):
        super().__init__(config)
        self._interface = None
        self._receive_queue: asyncio.Queue[MeshPacket] = asyncio.Queue()
        self._connected = False
        self._loop = None

    async def connect(self) -> bool:
        try:
            import meshtastic
            import meshtastic.serial_interface

            device = self.config.device or None  # None = auto-detect
            logger.info(f"Connecting to Meshtastic USB device: {device or 'auto-detect'}")

            self._loop = asyncio.get_event_loop()
            self._interface = await self._loop.run_in_executor(
                None,
                lambda: meshtastic.serial_interface.SerialInterface(device)
            )

            # Subscribe to received packets
            from pubsub import pub
            pub.subscribe(self._on_receive, "meshtastic.receive")
            pub.subscribe(self._on_connection, "meshtastic.connection.established")

            self._connected = True
            logger.info("Connected to Meshtastic USB device")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Meshtastic USB: {e}")
            self._connected = False
            return False

    async def disconnect(self) -> None:
        if self._interface:
            try:
                from pubsub import pub
                pub.unsubscribe(self._on_receive, "meshtastic.receive")
            except Exception:
                pass
            try:
                await self._loop.run_in_executor(None, self._interface.close)
            except Exception:
                pass
            self._interface = None
        self._connected = False
        logger.info("Disconnected from Meshtastic USB device")

    async def send_text(self, text: str, destination: int = MeshPacket.BROADCAST,
                        channel: int = 0) -> bool:
        if not self._interface:
            logger.error("Not connected")
            return False
        try:
            dest = destination if destination != MeshPacket.BROADCAST else "^all"
            await self._loop.run_in_executor(
                None,
                lambda: self._interface.sendText(
                    text,
                    destinationId=dest,
                    channelIndex=channel
                )
            )
            logger.info(f"Sent text to {destination:#010x}: {text[:50]}")
            return True
        except Exception as e:
            logger.error(f"Failed to send text: {e}")
            return False

    async def send_raw(self, dest: int, payload: bytes, port_num: PortNum,
                       channel: int = 0, want_ack: bool = False) -> bool:
        if not self._interface:
            logger.error("Not connected")
            return False
        try:
            await self._loop.run_in_executor(
                None,
                lambda: self._interface.sendData(
                    payload,
                    destinationId=dest,
                    portNum=port_num.value,
                    channelIndex=channel,
                    wantAck=want_ack
                )
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send raw packet: {e}")
            return False

    async def get_nodes(self) -> list[NodeInfo]:
        if not self._interface:
            return []
        try:
            nodes = []
            node_db = self._interface.nodes or {}
            for node_id_str, node_data in node_db.items():
                nodes.append(self._parse_node(node_data))
            return nodes
        except Exception as e:
            logger.error(f"Failed to get nodes: {e}")
            return []

    async def get_my_info(self) -> NodeInfo:
        if not self._interface:
            return NodeInfo(node_id=0, long_name="disconnected")
        try:
            my_info = self._interface.myInfo
            my_node = self._interface.nodes.get(f"!{my_info.my_node_num:08x}", {})
            return self._parse_node(my_node)
        except Exception as e:
            logger.error(f"Failed to get my info: {e}")
            return NodeInfo(node_id=0, long_name="error")

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
        return self._connected and self._interface is not None

    def _on_receive(self, packet, interface=None):
        """Callback from meshtastic-python pubsub (runs in meshtastic thread)."""
        try:
            mesh_packet = self._parse_packet(packet)
            if mesh_packet and self._loop:
                self._loop.call_soon_threadsafe(
                    self._receive_queue.put_nowait, mesh_packet
                )
                # Also notify async callbacks
                for cb in self._message_callbacks:
                    self._loop.call_soon_threadsafe(
                        asyncio.ensure_future, cb(mesh_packet)
                    )
        except Exception as e:
            logger.error(f"Error parsing received packet: {e}")

    def _on_connection(self, interface=None, topic=None):
        """Connection established callback."""
        logger.info("Meshtastic connection established")
        self._connected = True

    @staticmethod
    def _parse_packet(raw: dict) -> Optional[MeshPacket]:
        """Parse a meshtastic-python packet dict into our MeshPacket."""
        try:
            decoded = raw.get("decoded", {})
            port_num_val = decoded.get("portnum", 0)

            # Map string portnum to our enum
            port_map = {
                "TEXT_MESSAGE_APP": PortNum.TEXT_MESSAGE,
                "POSITION_APP": PortNum.POSITION,
                "NODEINFO_APP": PortNum.NODEINFO,
                "ROUTING_APP": PortNum.ROUTING,
                "TELEMETRY_APP": PortNum.TELEMETRY,
                "ADMIN_APP": PortNum.ADMIN,
            }

            if isinstance(port_num_val, str):
                port_num = port_map.get(port_num_val, PortNum.TEXT_MESSAGE)
            else:
                try:
                    port_num = PortNum(port_num_val)
                except ValueError:
                    port_num = PortNum.TEXT_MESSAGE

            packet = MeshPacket(
                source=raw.get("fromId", raw.get("from", 0)),
                destination=raw.get("toId", raw.get("to", 0)),
                packet_id=raw.get("id", 0),
                port_num=port_num,
                payload=decoded.get("payload", b""),
                channel=raw.get("channel", 0),
                hop_limit=raw.get("hopLimit", 3),
                hop_start=raw.get("hopStart", 3),
                want_ack=raw.get("wantAck", False),
                rx_snr=raw.get("rxSnr"),
                rx_rssi=raw.get("rxRssi"),
            )

            # Decode text messages
            if port_num == PortNum.TEXT_MESSAGE:
                text = decoded.get("text")
                if text:
                    packet.text = text

            # Decode position
            if port_num == PortNum.POSITION:
                pos = decoded.get("position", {})
                packet.position = pos

            return packet

        except Exception as e:
            logger.error(f"Failed to parse packet: {e}")
            return None

    @staticmethod
    def _parse_node(node_data: dict) -> NodeInfo:
        """Parse a meshtastic-python node dict into our NodeInfo."""
        user = node_data.get("user", {})
        position = node_data.get("position", {})
        metrics = node_data.get("deviceMetrics", {})

        node_num = node_data.get("num", 0)
        if isinstance(node_num, str) and node_num.startswith("!"):
            node_num = int(node_num[1:], 16)

        return NodeInfo(
            node_id=node_num,
            long_name=user.get("longName", ""),
            short_name=user.get("shortName", ""),
            hardware_model=user.get("hwModel", "UNKNOWN"),
            latitude=position.get("latitude"),
            longitude=position.get("longitude"),
            altitude=position.get("altitude"),
            battery_level=metrics.get("batteryLevel"),
            voltage=metrics.get("voltage"),
            snr=node_data.get("snr"),
            last_heard=node_data.get("lastHeard"),
            hops_away=node_data.get("hopsAway"),
        )
