"""
Meshtastic packet encoding/decoding.

Implements the Meshtastic wire format:
  [4-byte header][encrypted protobuf payload]

Header layout (big-endian):
  Bytes 0-3:  Destination node ID
  Bytes 4-7:  Source node ID
  Bytes 8-11: Packet ID
  Byte  12:   Flags (want_ack=0x08, via_mqtt=0x04, hop_limit=0x07)
  Byte  13:   Hash (channel + hop_start)
  Bytes 14-15: Reserved / next hop

AES-256-CTR encryption:
  Key:   SHA256 of channel name (default "LongFast" → fixed key)
  Nonce: [packet_id (4B LE)][source_id (4B LE)][0x00 * 8]
"""

import hashlib
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Optional


# ─── Channel Keys ────────────────────────────────────────────────────────────

# Default Meshtastic channel keys (SHA256 of channel name PSK)
# From: https://github.com/meshtastic/firmware/blob/master/src/mesh/CryptoEngine.cpp
CHANNEL_KEYS = {
    "OryahComms": bytes.fromhex("fd86f2f9384b2e71c0f4eb9240204a3000000000000000000000000000000000"),  # 128-bit zero-padded to 256
    "LongFast":  bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901"),  # Default 16-byte key
    # Full 32-byte default key for LongFast:
}

# The actual default Meshtastic PSK (from firmware)
# This is the well-known default PSK for the default channel
DEFAULT_PSK = bytes([
    0xd4, 0xf1, 0xbb, 0x3a, 0x20, 0x29, 0x07, 0x59,
    0xf0, 0xbc, 0xff, 0xab, 0xcf, 0x4e, 0x69, 0x01,
    0xd4, 0xf1, 0xbb, 0x3a, 0x20, 0x29, 0x07, 0x59,
    0xf0, 0xbc, 0xff, 0xab, 0xcf, 0x4e, 0x69, 0x01,
])


def get_channel_key(channel_name: str) -> bytes:
    """Get the 32-byte AES key for a channel (zero-padded to 32 bytes per Meshtastic firmware)."""
    if channel_name == "LongFast":
        return DEFAULT_PSK
    if channel_name in CHANNEL_KEYS:
        k = CHANNEL_KEYS[channel_name]
        return k + bytes(32 - len(k)) if len(k) < 32 else k
    # Unknown channel: derive from SHA256
    return hashlib.sha256(channel_name.encode()).digest()


# ─── AES-256-CTR ─────────────────────────────────────────────────────────────

def aes_ctr_crypt(data: bytes, key: bytes, packet_id: int, source_id: int) -> bytes:
    """
    AES-256-CTR encrypt/decrypt (symmetric).
    Nonce: packet_id (4B LE) + source_id (4B LE) + 8 zero bytes
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    nonce = struct.pack("<II", packet_id, source_id) + b'\x00' * 8  # 16 bytes
    cipher = Cipher(
        algorithms.AES(key),
        modes.CTR(nonce),
        backend=default_backend()
    )
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


# ─── Minimal Protobuf ─────────────────────────────────────────────────────────

def encode_varint(value: int) -> bytes:
    parts = []
    while value > 0x7F:
        parts.append((value & 0x7F) | 0x80)
        value >>= 7
    parts.append(value & 0x7F)
    return bytes(parts)


def encode_bytes_field(field_num: int, data: bytes) -> bytes:
    tag = (field_num << 3) | 2
    return encode_varint(tag) + encode_varint(len(data)) + data


def encode_varint_field(field_num: int, value: int) -> bytes:
    tag = (field_num << 3) | 0
    return encode_varint(tag) + encode_varint(value)


def encode_string_field(field_num: int, s: str) -> bytes:
    return encode_bytes_field(field_num, s.encode())


# ─── Meshtastic Protobuf ─────────────────────────────────────────────────────

def encode_data_payload(text: str) -> bytes:
    """
    Encode a Data protobuf (portnum=TEXT_MESSAGE_APP=1, payload=text bytes).
    """
    # message Data { uint32 portnum=1; bytes payload=2; }
    out = b''
    out += encode_varint_field(1, 1)           # portnum = TEXT_MESSAGE_APP
    out += encode_bytes_field(2, text.encode()) # payload
    return out


def encode_mesh_packet_inner(
    source: int,
    dest: int,
    packet_id: int,
    data_pb: bytes,
    hop_limit: int = 3,
) -> bytes:
    """
    Encode the inner MeshPacket fields that get encrypted.
    message MeshPacket { ... bytes encrypted=8; ... }
    We build the 'decoded' sub-message which then gets encrypted.
    """
    # message MeshPacket inner (the part that gets encrypted)
    # Actually we encrypt the Data payload directly, not the whole MeshPacket
    return data_pb


# ─── Wire Frame ──────────────────────────────────────────────────────────────

BROADCAST_ADDR = 0xFFFFFFFF


def build_packet(
    text: str,
    source_id: int,
    dest_id: int = BROADCAST_ADDR,
    channel_name: str = "LongFast",
    hop_limit: int = 3,
    packet_id: Optional[int] = None,
) -> bytes:
    """
    Build a complete Meshtastic LoRa wire frame.

    Returns raw bytes ready to send as LoRa PHY payload.

    Frame format:
      [dest:4][src:4][packet_id:4][flags:1][channel_hash:1][reserved:2][encrypted_payload]
    """
    if packet_id is None:
        packet_id = int(time.time() * 1000) & 0xFFFFFFFF

    # Build Data protobuf
    data_pb = encode_data_payload(text)

    # Encrypt with channel key
    key = get_channel_key(channel_name)
    encrypted = aes_ctr_crypt(data_pb, key, packet_id, source_id)

    # Build flags byte: hop_limit in low 3 bits, want_ack=0
    flags = hop_limit & 0x07

    # Channel hash: XOR of PSK bytes (matches Meshtastic firmware generateHash())
    key = get_channel_key(channel_name)
    ch_hash = 0
    for b in key[:16]:  # firmware uses psk.length (original key length, not expanded)
        ch_hash ^= b
    ch_hash &= 0xFF

    # Build header (all little-endian per Meshtastic spec)
    header = struct.pack("<IIIBBBH",
        dest_id,
        source_id,
        packet_id,
        flags,
        ch_hash,
        0x00,   # hop_start (will be overwritten to hop_limit on TX)
        0x0000, # reserved
    )

    # Wait — header is actually:
    # dest(4) + src(4) + packet_id(4) + flags(1) + channel_hash(1) + next_hop(1) + hop_start(1)
    header = struct.pack("<IIIBBBBB",
        dest_id & 0xFFFFFFFF,
        source_id & 0xFFFFFFFF,
        packet_id & 0xFFFFFFFF,
        flags,
        ch_hash,
        0x00,        # next hop
        hop_limit,   # hop_start
        0x00,        # padding
    )

    # Trim padding — Meshtastic header is exactly 16 bytes
    # dest(4) + src(4) + packet_id(4) + flags(1) + ch_hash(1) + next_hop(1) + hop_start(1) = 16
    header = struct.pack("<IIIB",
        dest_id & 0xFFFFFFFF,
        source_id & 0xFFFFFFFF,
        packet_id & 0xFFFFFFFF,
        flags | (hop_limit << 5),  # flags[2:0]=hop_limit, flags[5:3]=hop_start
    )

    # Actually, let me use the exact Meshtastic header format from the firmware:
    # typedef struct {
    #   NodeNum to;          // 4 bytes
    #   NodeNum from;        // 4 bytes
    #   PacketId id;         // 4 bytes
    #   uint8_t flags;       // hop_limit:3, want_ack:1, via_mqtt:1
    #   uint8_t channel;     // channel hash
    #   uint8_t next_hop;    
    #   uint8_t relay_node;  
    # } PacketHeader; // 16 bytes total

    header = struct.pack("<IIIBBBBB",
        dest_id & 0xFFFFFFFF,
        source_id & 0xFFFFFFFF,
        packet_id & 0xFFFFFFFF,
        (hop_limit & 0x07),  # flags: hop_limit in low 3 bits
        ch_hash,
        0x00,       # next_hop
        0x00,       # relay_node
        0x00,       # pad to align
    )
    # Header should be 4+4+4+1+1+1+1 = 16 bytes
    header = header[:16]

    return header + encrypted


# ─── US915 LongFast Frequencies ──────────────────────────────────────────────

# Meshtastic US915 LongFast TX frequencies (uplink channels)
US915_LONGFAST_FREQS = [
    903900000,
    904100000,
    904300000,
    904500000,
    904700000,
    904900000,
    905100000,
    905300000,
]

# LongFast modem config (from Meshtastic firmware)
LONGFAST_CONFIG = {
    "bandwidth":        250000,
    "spreading_factor": 11,
    "code_rate":        "4/8",
    "tx_power":         27,       # dBm
    "preamble_length":  16,
}


# Std LoRa channel freq — must match [gateway.concentrator.lora_std] in concentratord.toml
# Multi-SF channels are 125kHz (SX1302 HW limit); Meshtastic LongFast needs 250kHz.
STD_CHANNEL_FREQ = 906_875_000  # Hz

def get_tx_frequency(packet_id: int) -> int:
    """TX on the Std LoRa channel (250kHz SF11) so concentratord can also receive our packets."""
    return STD_CHANNEL_FREQ


# ─── DECODER ──────────────────────────────────────────────────────────────────

def read_varint(buf: bytes, pos: int):
    """Read a protobuf varint. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80): break
        shift += 7
    return result, pos

def read_field(buf: bytes, pos: int):
    """Read one protobuf field. Returns (field_num, wire_type, value, new_pos) or None."""
    import struct
    if pos >= len(buf): return None
    tag, pos = read_varint(buf, pos)
    fn = tag >> 3; wt = tag & 7
    if wt == 0:
        v, pos = read_varint(buf, pos)
        return fn, 0, v, pos
    elif wt == 1:
        v = struct.unpack_from('<q', buf, pos)[0]; return fn, 1, v, pos+8
    elif wt == 2:
        l, pos = read_varint(buf, pos); v = buf[pos:pos+l]; return fn, 2, v, pos+l
    elif wt == 5:
        v = struct.unpack_from('<I', buf, pos)[0]; return fn, 5, v, pos+4
    return None

def parse_fields(buf: bytes) -> dict:
    """Parse all protobuf fields in buf into a dict field_num→value (last wins)."""
    out = {}; pos = 0
    while pos < len(buf):
        r = read_field(buf, pos)
        if r is None: break
        fn, wt, v, pos = r
        out[fn] = v
    return out

def decode_packet(phy_payload: bytes, channel_name: str = "LongFast") -> dict:
    """
    Decode a received Meshtastic LoRa packet.
    Returns dict with: src, dst, packet_id, hop_limit, hop_start,
                       want_ack, portnum, text (if TEXT_MESSAGE), raw_payload, error
    """
    import struct

    result = {"error": None, "text": None, "portnum": None,
              "src": None, "dst": None, "packet_id": None}

    # Meshtastic LoRa PHY frame layout (little-endian):
    # [0..3]  dst (uint32 LE)
    # [4..7]  src (uint32 LE)
    # [8..11] packet_id (uint32 LE)
    # [12]    flags (hop_limit:3, want_ack:1, via_mqtt:1, hop_start:3)
    # [13]    channel hash (uint8)
    # [14..N] encrypted payload (varies)
    # Header: dst(4)+src(4)+packet_id(4)+flags(1)+chan_hash(1)+reserved(2) = 16 bytes
    if len(phy_payload) < 17:
        result["error"] = f"too short ({len(phy_payload)}B)"
        return result

    dst      = struct.unpack_from('<I', phy_payload, 0)[0]
    src      = struct.unpack_from('<I', phy_payload, 4)[0]
    pkt_id   = struct.unpack_from('<I', phy_payload, 8)[0]
    flags    = phy_payload[12]
    hop_limit  = flags & 0x07
    want_ack   = bool(flags & 0x08)
    via_mqtt   = bool(flags & 0x10)
    hop_start  = (flags >> 5) & 0x07
    chan_hash  = phy_payload[13]
    # bytes 14-15 = reserved
    encrypted  = phy_payload[16:]

    result.update({
        "dst": dst, "src": src, "packet_id": pkt_id,
        "hop_limit": hop_limit, "hop_start": hop_start,
        "want_ack": want_ack, "via_mqtt": via_mqtt,
        "chan_hash": chan_hash,
    })

    # Decrypt
    try:
        key = get_channel_key(channel_name)
        plaintext = aes_ctr_crypt(encrypted, key, pkt_id, src)
    except Exception as e:
        result["error"] = f"decrypt failed: {e}"
        return result

    # Parse Data protobuf (MeshPacket.decoded = Data)
    # Data fields: portnum=1(varint), payload=2(bytes), want_response=5(bool),
    #              dest=6, source=7, request_id=8, reply_id=9, emoji=10,
    #              bitfield=101
    try:
        fields = parse_fields(plaintext)
        portnum = fields.get(1, 0)
        payload = fields.get(2, b'')
        result["portnum"] = portnum

        # portnum 1 = TEXT_MESSAGE_APP
        if portnum == 1 and isinstance(payload, bytes):
            result["text"] = payload.decode('utf-8', errors='replace')

        # portnum 3 = POSITION_APP
        elif portnum == 3 and isinstance(payload, bytes):
            pf = parse_fields(payload)
            lat = pf.get(1, 0); lon = pf.get(2, 0); alt = pf.get(3, 0)
            # Meshtastic stores lat/lon as integer * 1e7
            if lat >= 2**31: lat -= 2**32
            if lon >= 2**31: lon -= 2**32
            result["position"] = {"lat": lat/1e7, "lon": lon/1e7, "alt": alt}

        # portnum 4 = NODEINFO_APP
        elif portnum == 4 and isinstance(payload, bytes):
            nf = parse_fields(payload)
            result["nodeinfo"] = {
                "id":       nf.get(1, b'').decode('utf-8', errors='replace') if isinstance(nf.get(1), bytes) else '',
                "longname": nf.get(2, b'').decode('utf-8', errors='replace') if isinstance(nf.get(2), bytes) else '',
                "shortname":nf.get(3, b'').decode('utf-8', errors='replace') if isinstance(nf.get(3), bytes) else '',
            }

        result["raw_payload"] = plaintext.hex()
    except Exception as e:
        result["error"] = f"protobuf parse failed: {e}"

    return result


PORTNUM_NAMES = {
    0: "UNKNOWN", 1: "TEXT", 2: "REMOTE_HARDWARE", 3: "POSITION",
    4: "NODEINFO", 5: "ROUTING", 6: "ADMIN", 7: "TEXT_BELL",
    32: "WAYPOINT", 33: "AUDIO", 34: "DETECTION_SENSOR",
    65: "REPLY", 66: "IP_TUNNEL", 67: "PAXCOUNTER",
    67: "SERIAL", 68: "STORE_FORWARD", 69: "RANGE_TEST",
    70: "TELEMETRY", 71: "ZPS", 72: "SIMULATOR", 73: "TRACEROUTE",
    74: "NEIGHBORINFO", 75: "ATAK", 76: "MAP_REPORT",
    256: "PRIVATE", 257: "ATAK_FORWARDER",
}
