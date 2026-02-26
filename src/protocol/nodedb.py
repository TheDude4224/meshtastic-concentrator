"""
Meshtastic node database.

Tracks known nodes on the mesh network with their metadata,
positions, and activity timestamps.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from .packets import MeshPacket, PortNum, Data, Position, User, decode_position, decode_nodeinfo


# Default expiry: nodes not heard for 2 hours are considered stale
DEFAULT_NODE_EXPIRY_SECONDS = 7200


@dataclass
class NodePosition:
    """Cached position for a node."""
    latitude_i: Optional[int] = None
    longitude_i: Optional[int] = None
    altitude: Optional[int] = None
    time: int = 0
    sats_in_view: int = 0

    @property
    def latitude(self) -> Optional[float]:
        return self.latitude_i * 1e-7 if self.latitude_i is not None else None

    @property
    def longitude(self) -> Optional[float]:
        return self.longitude_i * 1e-7 if self.longitude_i is not None else None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.latitude_i is not None:
            d["latitude_i"] = self.latitude_i
            d["latitude"] = self.latitude
        if self.longitude_i is not None:
            d["longitude_i"] = self.longitude_i
            d["longitude"] = self.longitude
        if self.altitude is not None:
            d["altitude"] = self.altitude
        if self.time:
            d["time"] = self.time
        if self.sats_in_view:
            d["sats_in_view"] = self.sats_in_view
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NodePosition":
        return cls(
            latitude_i=d.get("latitude_i"),
            longitude_i=d.get("longitude_i"),
            altitude=d.get("altitude"),
            time=d.get("time", 0),
            sats_in_view=d.get("sats_in_view", 0),
        )


@dataclass
class NodeInfo:
    """Information about a known mesh node."""
    node_id: int
    long_name: str = ""
    short_name: str = ""
    user_id: str = ""  # e.g. "!aabbccdd"
    hw_model: int = 0
    is_licensed: bool = False
    role: int = 0
    position: Optional[NodePosition] = None
    last_heard: float = 0.0
    snr: float = 0.0
    rssi: int = 0
    hop_limit: int = 0
    packet_count: int = 0

    @property
    def node_id_hex(self) -> str:
        """Node ID as hex string (e.g., '!aabbccdd')."""
        return f"!{self.node_id:08x}"

    @property
    def display_name(self) -> str:
        """Best available display name for this node."""
        if self.long_name:
            return self.long_name
        if self.short_name:
            return self.short_name
        return self.node_id_hex

    @property
    def is_stale(self) -> bool:
        """Check if node hasn't been heard recently."""
        if self.last_heard == 0:
            return True
        return (time.time() - self.last_heard) > DEFAULT_NODE_EXPIRY_SECONDS

    def seconds_since_heard(self) -> float:
        """Seconds since last packet from this node."""
        if self.last_heard == 0:
            return float("inf")
        return time.time() - self.last_heard

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for JSON export."""
        d: dict[str, Any] = {
            "node_id": self.node_id,
            "node_id_hex": self.node_id_hex,
        }
        if self.long_name:
            d["long_name"] = self.long_name
        if self.short_name:
            d["short_name"] = self.short_name
        if self.user_id:
            d["user_id"] = self.user_id
        if self.hw_model:
            d["hw_model"] = self.hw_model
        if self.is_licensed:
            d["is_licensed"] = True
        if self.role:
            d["role"] = self.role
        if self.position:
            d["position"] = self.position.to_dict()
        d["last_heard"] = self.last_heard
        d["snr"] = self.snr
        d["rssi"] = self.rssi
        d["hop_limit"] = self.hop_limit
        d["packet_count"] = self.packet_count
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NodeInfo":
        """Deserialize from dictionary."""
        pos = None
        if "position" in d:
            pos = NodePosition.from_dict(d["position"])
        return cls(
            node_id=d["node_id"],
            long_name=d.get("long_name", ""),
            short_name=d.get("short_name", ""),
            user_id=d.get("user_id", ""),
            hw_model=d.get("hw_model", 0),
            is_licensed=d.get("is_licensed", False),
            role=d.get("role", 0),
            position=pos,
            last_heard=d.get("last_heard", 0.0),
            snr=d.get("snr", 0.0),
            rssi=d.get("rssi", 0),
            hop_limit=d.get("hop_limit", 0),
            packet_count=d.get("packet_count", 0),
        )


class NodeDB:
    """
    Database of known mesh nodes.

    Tracks node metadata, positions, and activity. Supports JSON
    export/import for persistence.

    Usage:
        db = NodeDB()
        db.update_from_packet(packet)
        node = db.get_node(0x12345678)
        db.export_json("nodes.json")
    """

    def __init__(self, expiry_seconds: float = DEFAULT_NODE_EXPIRY_SECONDS) -> None:
        self._nodes: dict[int, NodeInfo] = {}
        self._expiry_seconds = expiry_seconds

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node_id: int) -> bool:
        return node_id in self._nodes

    def get_node(self, node_id: int) -> Optional[NodeInfo]:
        """Get node info by ID, or None if unknown."""
        return self._nodes.get(node_id)

    def get_or_create(self, node_id: int) -> NodeInfo:
        """Get existing node or create a new entry."""
        if node_id not in self._nodes:
            self._nodes[node_id] = NodeInfo(node_id=node_id)
        return self._nodes[node_id]

    def update_from_packet(self, packet: MeshPacket) -> Optional[NodeInfo]:
        """
        Update the node database from a received packet.

        Updates last_heard, SNR, RSSI for the source node.
        If the packet contains decoded NODEINFO or POSITION data,
        updates those fields too.

        Args:
            packet: Received and (optionally) decoded MeshPacket.

        Returns:
            The updated NodeInfo, or None if source is 0/broadcast.
        """
        source = packet.header.source
        if source == 0 or source == 0xFFFFFFFF:
            return None

        node = self.get_or_create(source)
        node.last_heard = time.time()
        node.snr = packet.rx_snr
        node.rssi = packet.rx_rssi
        node.hop_limit = packet.header.hop_limit
        node.packet_count += 1

        # If decoded, extract additional info
        if packet.decoded is not None:
            self._update_from_data(node, packet.decoded)

        return node

    def _update_from_data(self, node: NodeInfo, data: Data) -> None:
        """Update node info from decoded Data payload."""
        if data.portnum == PortNum.POSITION_APP and data.payload:
            try:
                pos = decode_position(data.payload)
                node.position = NodePosition(
                    latitude_i=pos.latitude_i,
                    longitude_i=pos.longitude_i,
                    altitude=pos.altitude,
                    time=pos.time,
                    sats_in_view=pos.sats_in_view,
                )
            except Exception:
                pass  # Malformed position, skip

        elif data.portnum == PortNum.NODEINFO_APP and data.payload:
            try:
                user = decode_nodeinfo(data.payload)
                if user.long_name:
                    node.long_name = user.long_name
                if user.short_name:
                    node.short_name = user.short_name
                if user.id:
                    node.user_id = user.id
                if user.hw_model:
                    node.hw_model = user.hw_model
                node.is_licensed = user.is_licensed
                if user.role:
                    node.role = user.role
            except Exception:
                pass  # Malformed nodeinfo, skip

    def expire_stale(self) -> list[int]:
        """
        Remove nodes that haven't been heard within the expiry window.

        Returns:
            List of removed node IDs.
        """
        cutoff = time.time() - self._expiry_seconds
        expired = [
            nid for nid, node in self._nodes.items()
            if node.last_heard > 0 and node.last_heard < cutoff
        ]
        for nid in expired:
            del self._nodes[nid]
        return expired

    def get_all_nodes(self, include_stale: bool = True) -> list[NodeInfo]:
        """
        Get all known nodes.

        Args:
            include_stale: If False, exclude nodes not heard recently.

        Returns:
            List of NodeInfo objects.
        """
        if include_stale:
            return list(self._nodes.values())
        return [n for n in self._nodes.values() if not n.is_stale]

    def get_nodes_with_position(self) -> list[NodeInfo]:
        """Get all nodes that have a known position."""
        return [n for n in self._nodes.values() if n.position is not None]

    def export_json(self, path: str | Path) -> None:
        """
        Export the node database to a JSON file.

        Args:
            path: File path to write.
        """
        data = {
            "exported_at": time.time(),
            "node_count": len(self._nodes),
            "nodes": {
                str(nid): node.to_dict()
                for nid, node in sorted(self._nodes.items())
            },
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def import_json(self, path: str | Path) -> int:
        """
        Import nodes from a JSON file (merges with existing data).

        Args:
            path: File path to read.

        Returns:
            Number of nodes imported.
        """
        path = Path(path)
        with open(path) as f:
            data = json.load(f)

        count = 0
        nodes_data = data.get("nodes", {})
        for _nid_str, node_dict in nodes_data.items():
            node = NodeInfo.from_dict(node_dict)
            # Merge: keep the more recent data
            existing = self._nodes.get(node.node_id)
            if existing is None or node.last_heard > existing.last_heard:
                self._nodes[node.node_id] = node
                count += 1
        return count

    def clear(self) -> None:
        """Remove all nodes from the database."""
        self._nodes.clear()

    def summary(self) -> str:
        """Return a human-readable summary of the node database."""
        total = len(self._nodes)
        active = len([n for n in self._nodes.values() if not n.is_stale])
        with_pos = len(self.get_nodes_with_position())
        return f"NodeDB: {total} nodes ({active} active, {with_pos} with position)"
