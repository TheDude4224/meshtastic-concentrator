"""
Meshtastic protocol implementation for concentrator gateway.

Pure Python implementation of Meshtastic packet encoding/decoding,
encryption, channel management, routing, and node tracking.
No dependency on meshtastic-python or hardware.
"""

from .packets import (
    PortNum,
    Data,
    MeshPacket,
    PacketHeader,
    encode_mesh_packet,
    decode_mesh_packet,
)
from .crypto import MeshtasticCrypto
from .channels import ChannelPreset, LoRaModulation, get_channel_config, get_frequency_hz
from .routing import MeshRouter
from .nodedb import NodeDB, NodeInfo

__all__ = [
    "PortNum",
    "Data",
    "MeshPacket",
    "PacketHeader",
    "encode_mesh_packet",
    "decode_mesh_packet",
    "MeshtasticCrypto",
    "ChannelPreset",
    "LoRaModulation",
    "get_channel_config",
    "get_frequency_hz",
    "MeshRouter",
    "NodeDB",
    "NodeInfo",
]
