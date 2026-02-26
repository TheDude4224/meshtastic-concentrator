"""
Abstract transport layer for Meshtastic mesh communication.

This abstraction allows swapping between:
- USB Meshtastic radio (prototype, via meshtastic-python)
- SX1302/1303 concentrator daemon (production)
- TCP/simulated transport (testing)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable
from enum import IntEnum
import time


class PortNum(IntEnum):
    """Meshtastic port numbers (app layer protocol identifiers)"""
    TEXT_MESSAGE = 1
    REMOTE_HARDWARE = 2
    POSITION = 3
    NODEINFO = 4
    ROUTING = 5
    ADMIN = 6
    TELEMETRY = 67
    RANGE_TEST = 73
    STORE_FORWARD = 74
    DETECTION_SENSOR = 206
    PRIVATE = 256


@dataclass
class NodeInfo:
    """Information about a mesh node"""
    node_id: int                    # Meshtastic numeric node ID
    long_name: str = ""             # Human-readable name
    short_name: str = ""            # 4-char short name
    hardware_model: str = "UNKNOWN"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[int] = None
    battery_level: Optional[int] = None  # 0-100
    voltage: Optional[float] = None
    snr: Optional[float] = None     # Signal-to-noise ratio
    rssi: Optional[int] = None      # Received signal strength
    last_heard: Optional[float] = None  # Unix timestamp
    hops_away: Optional[int] = None
    is_concentrator: bool = False    # True if this is one of our super-nodes

    @property
    def hex_id(self) -> str:
        return f"!{self.node_id:08x}"

    @property
    def age_seconds(self) -> Optional[float]:
        if self.last_heard is None:
            return None
        return time.time() - self.last_heard


@dataclass
class MeshPacket:
    """A decoded mesh packet"""
    source: int                     # Sender node ID
    destination: int                # Destination node ID (0xFFFFFFFF = broadcast)
    packet_id: int                  # Unique packet ID
    port_num: PortNum               # Application port
    payload: bytes                  # Raw payload
    channel: int = 0               # Channel index
    hop_limit: int = 3             # Remaining hops
    hop_start: int = 3             # Original hop count
    want_ack: bool = False
    rx_time: Optional[float] = None
    rx_snr: Optional[float] = None
    rx_rssi: Optional[int] = None
    rx_channel: Optional[int] = None  # IF channel (concentrator: 0-7)

    # Decoded convenience fields (populated by protocol layer)
    text: Optional[str] = None      # If TEXT_MESSAGE
    position: Optional[dict] = None # If POSITION
    telemetry: Optional[dict] = None # If TELEMETRY

    BROADCAST = 0xFFFFFFFF


@dataclass
class TransportConfig:
    """Configuration for a mesh transport"""
    # Connection
    device: str = ""                # USB serial port, TCP address, or socket path
    transport_type: str = "usb"     # "usb", "concentrator", "tcp", "simulated"

    # Radio settings (for concentrator)
    frequency: float = 906.875      # MHz (US915 LongFast slot 0)
    bandwidth: int = 250000         # Hz
    spreading_factor: int = 11      # LongFast default
    coding_rate: int = 8            # 4/8
    tx_power: int = 30              # dBm

    # Mesh settings
    channel_name: str = "LongFast"
    channel_psk: Optional[bytes] = None  # None = default key
    hop_limit: int = 3
    region: str = "US"              # US, EU, etc.

    # Concentrator-specific
    num_rx_channels: int = 8        # SX1302/1303 IF channels
    spi_device: str = "/dev/spidev0.0"
    reset_pin: int = 17             # GPIO pin for SX1302 reset


class MeshTransport(ABC):
    """
    Abstract mesh transport.

    Implementations handle the actual radio communication.
    The OpenClaw skill and protocol layer use this interface
    without caring about the underlying hardware.
    """

    def __init__(self, config: TransportConfig):
        self.config = config
        self._message_callbacks: list[Callable[[MeshPacket], Awaitable[None]]] = []

    @abstractmethod
    async def connect(self) -> bool:
        """Connect to the radio/daemon. Returns True on success."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the radio/daemon."""
        ...

    @abstractmethod
    async def send_text(self, text: str, destination: int = MeshPacket.BROADCAST,
                        channel: int = 0) -> bool:
        """Send a text message. Returns True on success."""
        ...

    @abstractmethod
    async def send_raw(self, dest: int, payload: bytes, port_num: PortNum,
                       channel: int = 0, want_ack: bool = False) -> bool:
        """Send a raw packet. Returns True on success."""
        ...

    @abstractmethod
    async def get_nodes(self) -> list[NodeInfo]:
        """Get list of known mesh nodes."""
        ...

    @abstractmethod
    async def get_my_info(self) -> NodeInfo:
        """Get this node's info."""
        ...

    @abstractmethod
    async def receive(self, timeout: Optional[float] = None) -> Optional[MeshPacket]:
        """
        Wait for and return a mesh packet.
        Returns None on timeout.
        """
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if transport is currently connected."""
        ...

    def on_message(self, callback: Callable[[MeshPacket], Awaitable[None]]) -> None:
        """Register a callback for incoming messages."""
        self._message_callbacks.append(callback)

    async def _notify_callbacks(self, packet: MeshPacket) -> None:
        """Notify all registered callbacks of a new packet."""
        for cb in self._message_callbacks:
            try:
                await cb(packet)
            except Exception:
                pass  # Don't let one bad callback break everything
