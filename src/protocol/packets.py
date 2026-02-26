"""
Meshtastic packet encoding and decoding.

Implements manual protobuf encoding/decoding for MeshPacket and Data messages
matching the Meshtastic protobuf definitions, without requiring the meshtastic
pip package.

Over-the-air packet format (after LoRa PHY):
  - 4 bytes: destination (fixed32, little-endian)
  - 4 bytes: source/sender (fixed32, little-endian)
  - 4 bytes: packet ID (fixed32, little-endian)
  - 1 byte:  flags (bits 0-2: hop_limit, bit 3: want_ack, bits 4-7: channel hash or via_mqtt)
  - Remaining bytes: encrypted payload (Data protobuf, encrypted)

Total header = 13 bytes, followed by encrypted protobuf payload.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


# ---------------------------------------------------------------------------
# Protobuf wire-format helpers (subset needed for Meshtastic)
# ---------------------------------------------------------------------------

def _encode_varint(value: int) -> bytes:
    """Encode an unsigned integer as a protobuf varint."""
    if value < 0:
        # Protobuf treats negative varints as 10-byte two's complement
        value = value & 0xFFFFFFFFFFFFFFFF
    parts: list[int] = []
    while value > 0x7F:
        parts.append((value & 0x7F) | 0x80)
        value >>= 7
    parts.append(value & 0x7F)
    return bytes(parts)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a varint from data at offset. Returns (value, new_offset)."""
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("Truncated varint")
        b = data[offset]
        offset += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift >= 64:
            raise ValueError("Varint too long")
    return result, offset


def _encode_field_varint(field_num: int, value: int) -> bytes:
    """Encode a varint field (wire type 0)."""
    if value == 0:
        return b""
    tag = (field_num << 3) | 0
    return _encode_varint(tag) + _encode_varint(value)


def _encode_field_fixed32(field_num: int, value: int) -> bytes:
    """Encode a fixed32 field (wire type 5)."""
    tag = (field_num << 3) | 5
    return _encode_varint(tag) + struct.pack("<I", value & 0xFFFFFFFF)


def _encode_field_sfixed32(field_num: int, value: int) -> bytes:
    """Encode a sfixed32 field (wire type 5)."""
    tag = (field_num << 3) | 5
    return _encode_varint(tag) + struct.pack("<i", value)


def _encode_field_bytes(field_num: int, value: bytes) -> bytes:
    """Encode a length-delimited field (wire type 2)."""
    if not value:
        return b""
    tag = (field_num << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(value)) + value


def _encode_field_string(field_num: int, value: str) -> bytes:
    """Encode a string field (wire type 2)."""
    if not value:
        return b""
    encoded = value.encode("utf-8")
    return _encode_field_bytes(field_num, encoded)


def _encode_field_float(field_num: int, value: float) -> bytes:
    """Encode a float field (wire type 5)."""
    if value == 0.0:
        return b""
    tag = (field_num << 3) | 5
    return _encode_varint(tag) + struct.pack("<f", value)


def _encode_field_bool(field_num: int, value: bool) -> bytes:
    """Encode a bool field as varint."""
    return _encode_field_varint(field_num, 1 if value else 0)


def _encode_zigzag(value: int) -> int:
    """Encode a signed int as zigzag for sint32/sint64."""
    return (value << 1) ^ (value >> 31)


def _decode_zigzag(value: int) -> int:
    """Decode zigzag encoding back to signed int."""
    return (value >> 1) ^ -(value & 1)


# Wire types
WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LENGTH_DELIMITED = 2
WIRE_32BIT = 5


def _iter_fields(data: bytes) -> list[tuple[int, int, bytes | int]]:
    """
    Parse protobuf fields from raw bytes.
    Returns list of (field_number, wire_type, value).
    For varint: value is int.
    For fixed32/64: value is bytes (4 or 8 bytes).
    For length-delimited: value is bytes.
    """
    fields: list[tuple[int, int, bytes | int]] = []
    offset = 0
    while offset < len(data):
        tag, offset = _decode_varint(data, offset)
        field_num = tag >> 3
        wire_type = tag & 0x07

        if wire_type == WIRE_VARINT:
            value, offset = _decode_varint(data, offset)
            fields.append((field_num, wire_type, value))
        elif wire_type == WIRE_64BIT:
            fields.append((field_num, wire_type, data[offset:offset + 8]))
            offset += 8
        elif wire_type == WIRE_LENGTH_DELIMITED:
            length, offset = _decode_varint(data, offset)
            fields.append((field_num, wire_type, data[offset:offset + length]))
            offset += length
        elif wire_type == WIRE_32BIT:
            fields.append((field_num, wire_type, data[offset:offset + 4]))
            offset += 4
        else:
            raise ValueError(f"Unknown wire type {wire_type} for field {field_num}")
    return fields


def _fixed32_val(raw: bytes | int) -> int:
    """Extract uint32 from fixed32 field value."""
    if isinstance(raw, int):
        return raw
    return struct.unpack("<I", raw)[0]


def _sfixed32_val(raw: bytes | int) -> int:
    """Extract int32 from sfixed32 field value."""
    if isinstance(raw, int):
        return raw
    return struct.unpack("<i", raw)[0]


def _float_val(raw: bytes | int) -> float:
    """Extract float from fixed32 field value."""
    if isinstance(raw, int):
        return float(raw)
    return struct.unpack("<f", raw)[0]


# ---------------------------------------------------------------------------
# Meshtastic enums
# ---------------------------------------------------------------------------

class PortNum(IntEnum):
    """Meshtastic port numbers (subset relevant for concentrator)."""
    UNKNOWN_APP = 0
    TEXT_MESSAGE_APP = 1
    REMOTE_HARDWARE_APP = 2
    POSITION_APP = 3
    NODEINFO_APP = 4
    ROUTING_APP = 5
    ADMIN_APP = 6
    TEXT_MESSAGE_COMPRESSED_APP = 7
    WAYPOINT_APP = 8
    AUDIO_APP = 9
    DETECTION_SENSOR_APP = 10
    REPLY_APP = 32
    IP_TUNNEL_APP = 33
    PAXCOUNTER_APP = 34
    SERIAL_APP = 64
    STORE_FORWARD_APP = 65
    RANGE_TEST_APP = 66
    TELEMETRY_APP = 67
    ZPS_APP = 68
    SIMULATOR_APP = 69
    TRACEROUTE_APP = 70
    NEIGHBORINFO_APP = 71
    ATAK_PLUGIN = 72
    MAP_REPORT_APP = 73
    POWERSTRESS_APP = 74
    PRIVATE_APP = 256
    ATAK_FORWARDER = 257
    MAX = 511


# ---------------------------------------------------------------------------
# Data message (protobuf field IDs from mesh.proto)
# ---------------------------------------------------------------------------

@dataclass
class Data:
    """
    Meshtastic Data message (the decrypted payload inside a MeshPacket).

    Protobuf field mapping:
      1: portnum (varint/enum)
      2: payload (bytes)
      3: want_response (bool/varint)
      4: dest (fixed32)
      5: source (fixed32)
      6: request_id (fixed32)
      7: reply_id (fixed32)
      8: emoji (fixed32)
      9: bitfield (varint, optional)
    """
    portnum: PortNum = PortNum.UNKNOWN_APP
    payload: bytes = b""
    want_response: bool = False
    dest: int = 0
    source: int = 0
    request_id: int = 0
    reply_id: int = 0
    emoji: int = 0
    bitfield: Optional[int] = None

    def encode(self) -> bytes:
        """Encode Data message to protobuf bytes."""
        parts: list[bytes] = []
        parts.append(_encode_field_varint(1, int(self.portnum)))
        parts.append(_encode_field_bytes(2, self.payload))
        if self.want_response:
            parts.append(_encode_field_bool(3, True))
        if self.dest:
            parts.append(_encode_field_fixed32(4, self.dest))
        if self.source:
            parts.append(_encode_field_fixed32(5, self.source))
        if self.request_id:
            parts.append(_encode_field_fixed32(6, self.request_id))
        if self.reply_id:
            parts.append(_encode_field_fixed32(7, self.reply_id))
        if self.emoji:
            parts.append(_encode_field_fixed32(8, self.emoji))
        if self.bitfield is not None:
            parts.append(_encode_field_varint(9, self.bitfield))
        return b"".join(parts)

    @classmethod
    def decode(cls, data: bytes) -> "Data":
        """Decode Data message from protobuf bytes."""
        msg = cls()
        for field_num, wire_type, value in _iter_fields(data):
            if field_num == 1 and wire_type == WIRE_VARINT:
                try:
                    msg.portnum = PortNum(value)
                except ValueError:
                    msg.portnum = PortNum.UNKNOWN_APP
            elif field_num == 2 and wire_type == WIRE_LENGTH_DELIMITED:
                msg.payload = value  # type: ignore[assignment]
            elif field_num == 3 and wire_type == WIRE_VARINT:
                msg.want_response = bool(value)
            elif field_num == 4 and wire_type == WIRE_32BIT:
                msg.dest = _fixed32_val(value)
            elif field_num == 5 and wire_type == WIRE_32BIT:
                msg.source = _fixed32_val(value)
            elif field_num == 6 and wire_type == WIRE_32BIT:
                msg.request_id = _fixed32_val(value)
            elif field_num == 7 and wire_type == WIRE_32BIT:
                msg.reply_id = _fixed32_val(value)
            elif field_num == 8 and wire_type == WIRE_32BIT:
                msg.emoji = _fixed32_val(value)
            elif field_num == 9 and wire_type == WIRE_VARINT:
                msg.bitfield = value
        return msg

    def get_text(self) -> Optional[str]:
        """If this is a TEXT_MESSAGE, return the payload as UTF-8 string."""
        if self.portnum == PortNum.TEXT_MESSAGE_APP:
            return self.payload.decode("utf-8", errors="replace")
        return None


# ---------------------------------------------------------------------------
# Position message decoder (subset)
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """Decoded Meshtastic Position message (subset of fields)."""
    latitude_i: Optional[int] = None  # 1e-7 degrees
    longitude_i: Optional[int] = None
    altitude: Optional[int] = None  # meters MSL
    time: int = 0  # epoch seconds
    sats_in_view: int = 0
    precision_bits: int = 0

    @property
    def latitude(self) -> Optional[float]:
        return self.latitude_i * 1e-7 if self.latitude_i is not None else None

    @property
    def longitude(self) -> Optional[float]:
        return self.longitude_i * 1e-7 if self.longitude_i is not None else None

    def encode(self) -> bytes:
        """Encode Position to protobuf bytes."""
        parts: list[bytes] = []
        if self.latitude_i is not None:
            parts.append(_encode_field_sfixed32(1, self.latitude_i))
        if self.longitude_i is not None:
            parts.append(_encode_field_sfixed32(2, self.longitude_i))
        if self.altitude is not None:
            parts.append(_encode_field_varint(3, self.altitude))
        if self.time:
            parts.append(_encode_field_fixed32(4, self.time))
        if self.sats_in_view:
            parts.append(_encode_field_varint(19, self.sats_in_view))
        if self.precision_bits:
            parts.append(_encode_field_varint(23, self.precision_bits))
        return b"".join(parts)

    @classmethod
    def decode(cls, data: bytes) -> "Position":
        """Decode Position from protobuf bytes."""
        msg = cls()
        for field_num, wire_type, value in _iter_fields(data):
            if field_num == 1 and wire_type == WIRE_32BIT:
                msg.latitude_i = _sfixed32_val(value)
            elif field_num == 2 and wire_type == WIRE_32BIT:
                msg.longitude_i = _sfixed32_val(value)
            elif field_num == 3 and wire_type == WIRE_VARINT:
                msg.altitude = value
            elif field_num == 4 and wire_type == WIRE_32BIT:
                msg.time = _fixed32_val(value)
            elif field_num == 19 and wire_type == WIRE_VARINT:
                msg.sats_in_view = value
            elif field_num == 23 and wire_type == WIRE_VARINT:
                msg.precision_bits = value
        return msg


# ---------------------------------------------------------------------------
# User / NodeInfo message decoder (subset)
# ---------------------------------------------------------------------------

@dataclass
class User:
    """Decoded Meshtastic User message."""
    id: str = ""
    long_name: str = ""
    short_name: str = ""
    hw_model: int = 0
    is_licensed: bool = False
    role: int = 0
    public_key: bytes = b""

    def encode(self) -> bytes:
        parts: list[bytes] = []
        parts.append(_encode_field_string(1, self.id))
        parts.append(_encode_field_string(2, self.long_name))
        parts.append(_encode_field_string(3, self.short_name))
        if self.hw_model:
            parts.append(_encode_field_varint(5, self.hw_model))
        if self.is_licensed:
            parts.append(_encode_field_bool(6, True))
        if self.role:
            parts.append(_encode_field_varint(7, self.role))
        if self.public_key:
            parts.append(_encode_field_bytes(8, self.public_key))
        return b"".join(parts)

    @classmethod
    def decode(cls, data: bytes) -> "User":
        msg = cls()
        for field_num, wire_type, value in _iter_fields(data):
            if field_num == 1 and wire_type == WIRE_LENGTH_DELIMITED:
                msg.id = value.decode("utf-8", errors="replace")  # type: ignore[union-attr]
            elif field_num == 2 and wire_type == WIRE_LENGTH_DELIMITED:
                msg.long_name = value.decode("utf-8", errors="replace")  # type: ignore[union-attr]
            elif field_num == 3 and wire_type == WIRE_LENGTH_DELIMITED:
                msg.short_name = value.decode("utf-8", errors="replace")  # type: ignore[union-attr]
            elif field_num == 5 and wire_type == WIRE_VARINT:
                msg.hw_model = value  # type: ignore[assignment]
            elif field_num == 6 and wire_type == WIRE_VARINT:
                msg.is_licensed = bool(value)
            elif field_num == 7 and wire_type == WIRE_VARINT:
                msg.role = value  # type: ignore[assignment]
            elif field_num == 8 and wire_type == WIRE_LENGTH_DELIMITED:
                msg.public_key = value  # type: ignore[assignment]
        return msg


# ---------------------------------------------------------------------------
# Over-the-air packet header
# ---------------------------------------------------------------------------

BROADCAST_ADDR = 0xFFFFFFFF
HEADER_SIZE = 13  # 4 + 4 + 4 + 1 bytes


@dataclass
class PacketHeader:
    """
    Meshtastic over-the-air LoRa packet header (13 bytes).

    Format:
      [0:4]   destination node ID (little-endian uint32)
      [4:8]   source node ID (little-endian uint32)
      [8:12]  packet ID (little-endian uint32)
      [12]    flags byte:
              bits 0-2: hop_limit (0-7)
              bit 3:    want_ack
              bits 4-7: channel_hash (4-bit, used to select decryption key)
    """
    destination: int = BROADCAST_ADDR
    source: int = 0
    packet_id: int = 0
    hop_limit: int = 3
    want_ack: bool = False
    channel_hash: int = 0  # 4-bit channel identifier

    @property
    def flags(self) -> int:
        """Construct the flags byte."""
        f = self.hop_limit & 0x07
        if self.want_ack:
            f |= 0x08
        f |= (self.channel_hash & 0x0F) << 4
        return f

    @flags.setter
    def flags(self, value: int) -> None:
        """Parse the flags byte."""
        self.hop_limit = value & 0x07
        self.want_ack = bool(value & 0x08)
        self.channel_hash = (value >> 4) & 0x0F

    def encode(self) -> bytes:
        """Encode header to 13 bytes."""
        return struct.pack("<III", self.destination, self.source, self.packet_id) + bytes([self.flags])

    @classmethod
    def decode(cls, data: bytes) -> "PacketHeader":
        """Decode 13-byte header."""
        if len(data) < HEADER_SIZE:
            raise ValueError(f"Header too short: {len(data)} bytes, need {HEADER_SIZE}")
        dest, src, pkt_id = struct.unpack("<III", data[:12])
        hdr = cls(destination=dest, source=src, packet_id=pkt_id)
        hdr.flags = data[12]
        return hdr

    @property
    def is_broadcast(self) -> bool:
        return self.destination == BROADCAST_ADDR


# ---------------------------------------------------------------------------
# MeshPacket (combined header + payload)
# ---------------------------------------------------------------------------

@dataclass
class MeshPacket:
    """
    Complete Meshtastic mesh packet as seen over the air.

    Contains the header fields and either encrypted bytes or decoded Data.
    """
    header: PacketHeader = field(default_factory=PacketHeader)
    encrypted: Optional[bytes] = None  # raw encrypted payload (before decryption)
    decoded: Optional[Data] = None  # decrypted and parsed Data message

    # Metadata (not sent over the air, populated on reception)
    rx_time: int = 0
    rx_snr: float = 0.0
    rx_rssi: int = 0

    @property
    def from_id(self) -> int:
        return self.header.source

    @property
    def to_id(self) -> int:
        return self.header.destination

    @property
    def packet_id(self) -> int:
        return self.header.packet_id

    @property
    def hop_limit(self) -> int:
        return self.header.hop_limit

    @property
    def channel_hash(self) -> int:
        return self.header.channel_hash

    def to_protobuf(self) -> bytes:
        """
        Encode to protobuf MeshPacket format (for MQTT / API use).
        Uses the protobuf field numbers from mesh.proto.
        """
        parts: list[bytes] = []
        parts.append(_encode_field_fixed32(1, self.header.source))  # from
        parts.append(_encode_field_fixed32(2, self.header.destination))  # to
        parts.append(_encode_field_varint(3, self.header.channel_hash))  # channel
        if self.decoded is not None:
            parts.append(_encode_field_bytes(4, self.decoded.encode()))  # decoded
        elif self.encrypted is not None:
            parts.append(_encode_field_bytes(5, self.encrypted))  # encrypted
        parts.append(_encode_field_fixed32(6, self.header.packet_id))  # id
        if self.rx_time:
            parts.append(_encode_field_fixed32(7, self.rx_time))
        if self.rx_snr != 0.0:
            parts.append(_encode_field_float(8, self.rx_snr))
        parts.append(_encode_field_varint(9, self.header.hop_limit))
        if self.header.want_ack:
            parts.append(_encode_field_bool(10, True))
        if self.rx_rssi:
            # field 12, varint (int32 encoded as signed)
            parts.append(_encode_field_varint(12, self.rx_rssi & 0xFFFFFFFF))
        return b"".join(parts)

    @classmethod
    def from_protobuf(cls, data: bytes) -> "MeshPacket":
        """Decode a protobuf-encoded MeshPacket (from MQTT / API)."""
        pkt = cls()
        for field_num, wire_type, value in _iter_fields(data):
            if field_num == 1 and wire_type == WIRE_32BIT:
                pkt.header.source = _fixed32_val(value)
            elif field_num == 2 and wire_type == WIRE_32BIT:
                pkt.header.destination = _fixed32_val(value)
            elif field_num == 3 and wire_type == WIRE_VARINT:
                pkt.header.channel_hash = value  # type: ignore[assignment]
            elif field_num == 4 and wire_type == WIRE_LENGTH_DELIMITED:
                pkt.decoded = Data.decode(value)  # type: ignore[arg-type]
            elif field_num == 5 and wire_type == WIRE_LENGTH_DELIMITED:
                pkt.encrypted = value  # type: ignore[assignment]
            elif field_num == 6 and wire_type == WIRE_32BIT:
                pkt.header.packet_id = _fixed32_val(value)
            elif field_num == 7 and wire_type == WIRE_32BIT:
                pkt.rx_time = _fixed32_val(value)
            elif field_num == 8 and wire_type == WIRE_32BIT:
                pkt.rx_snr = _float_val(value)
            elif field_num == 9 and wire_type == WIRE_VARINT:
                pkt.header.hop_limit = value  # type: ignore[assignment]
            elif field_num == 10 and wire_type == WIRE_VARINT:
                pkt.header.want_ack = bool(value)
            elif field_num == 12 and wire_type == WIRE_VARINT:
                pkt.rx_rssi = value  # type: ignore[assignment]
        return pkt


# ---------------------------------------------------------------------------
# Raw LoRa packet encode/decode (over-the-air format)
# ---------------------------------------------------------------------------

def encode_mesh_packet(header: PacketHeader, payload: bytes) -> bytes:
    """
    Encode a complete over-the-air LoRa packet.

    Args:
        header: The 13-byte packet header.
        payload: The (already encrypted) payload bytes.

    Returns:
        Complete packet bytes ready for LoRa transmission.
    """
    return header.encode() + payload


def decode_mesh_packet(raw: bytes) -> MeshPacket:
    """
    Decode a raw over-the-air LoRa packet into a MeshPacket.

    The payload remains encrypted; use MeshtasticCrypto to decrypt,
    then Data.decode() to parse the protobuf.

    Args:
        raw: Raw bytes received from LoRa radio.

    Returns:
        MeshPacket with header parsed and encrypted payload stored.

    Raises:
        ValueError: If packet is too short.
    """
    if len(raw) < HEADER_SIZE:
        raise ValueError(f"Packet too short: {len(raw)} bytes, minimum {HEADER_SIZE}")

    header = PacketHeader.decode(raw[:HEADER_SIZE])
    encrypted = raw[HEADER_SIZE:]

    return MeshPacket(
        header=header,
        encrypted=encrypted if encrypted else None,
    )


# ---------------------------------------------------------------------------
# Payload decoders for common port types
# ---------------------------------------------------------------------------

def decode_position(payload: bytes) -> Position:
    """Decode a POSITION_APP payload."""
    return Position.decode(payload)


def decode_nodeinfo(payload: bytes) -> User:
    """Decode a NODEINFO_APP payload (User message)."""
    return User.decode(payload)
