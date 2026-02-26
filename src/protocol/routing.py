"""
Meshtastic mesh routing logic.

Implements flood routing with duplicate detection, hop limit management,
and rebroadcast decision logic matching the Meshtastic firmware behavior.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .packets import MeshPacket, PacketHeader, BROADCAST_ADDR


# Default TTL for seen packets cache (5 minutes matches firmware)
DEFAULT_SEEN_TTL_SECONDS = 300

# Default max hop limit
DEFAULT_MAX_HOP_LIMIT = 7


@dataclass
class SeenPacket:
    """Record of a previously seen packet."""
    sender: int
    packet_id: int
    first_seen: float
    hop_limit_at_receipt: int
    count: int = 1  # how many times we've seen this packet


@dataclass
class NeighborInfo:
    """Information about a direct neighbor node."""
    node_id: int
    last_heard: float = 0.0
    snr: float = 0.0
    rssi: int = 0
    packet_count: int = 0

    @property
    def is_stale(self) -> bool:
        """Consider neighbor stale after 2 hours with no packets."""
        return (time.time() - self.last_heard) > 7200


class MeshRouter:
    """
    Meshtastic flood router implementation.

    Handles:
    - Duplicate packet detection via (sender, packet_id) cache
    - Hop limit management
    - Rebroadcast decisions for flood routing
    - Neighbor tracking

    Usage:
        router = MeshRouter(my_node_id=0x12345678)

        # When receiving a packet:
        if router.should_accept(packet):
            # Process packet...
            if router.should_rebroadcast(packet):
                rebroadcast_packet = router.prepare_rebroadcast(packet)
                # Send rebroadcast_packet over radio
    """

    def __init__(
        self,
        my_node_id: int = 0,
        seen_ttl: float = DEFAULT_SEEN_TTL_SECONDS,
        max_hop_limit: int = DEFAULT_MAX_HOP_LIMIT,
    ) -> None:
        """
        Initialize the mesh router.

        Args:
            my_node_id: This node's ID (0 = concentrator/observer mode).
            seen_ttl: Time in seconds to keep packets in the seen cache.
            max_hop_limit: Maximum hop limit to allow.
        """
        self.my_node_id = my_node_id
        self.seen_ttl = seen_ttl
        self.max_hop_limit = max_hop_limit

        # Cache of seen packets: key = (sender_id, packet_id)
        self._seen: dict[tuple[int, int], SeenPacket] = {}
        self._last_cleanup: float = time.time()
        self._cleanup_interval: float = 60.0  # cleanup every 60s

        # Direct neighbor tracking
        self._neighbors: dict[int, NeighborInfo] = {}

    def _cleanup_seen(self) -> None:
        """Remove expired entries from the seen packet cache."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return

        cutoff = now - self.seen_ttl
        expired = [k for k, v in self._seen.items() if v.first_seen < cutoff]
        for k in expired:
            del self._seen[k]
        self._last_cleanup = now

    def _is_duplicate(self, sender: int, packet_id: int) -> bool:
        """
        Check if we've already seen this (sender, packet_id) combination.

        Args:
            sender: Source node ID.
            packet_id: Packet identifier.

        Returns:
            True if this is a duplicate.
        """
        self._cleanup_seen()
        key = (sender, packet_id)
        if key in self._seen:
            self._seen[key].count += 1
            return True
        return False

    def _mark_seen(self, sender: int, packet_id: int, hop_limit: int) -> None:
        """Record a packet as seen."""
        key = (sender, packet_id)
        self._seen[key] = SeenPacket(
            sender=sender,
            packet_id=packet_id,
            first_seen=time.time(),
            hop_limit_at_receipt=hop_limit,
        )

    def _update_neighbor(
        self, node_id: int, snr: float = 0.0, rssi: int = 0
    ) -> None:
        """Update neighbor information from a received packet."""
        if node_id == 0 or node_id == BROADCAST_ADDR:
            return

        now = time.time()
        if node_id in self._neighbors:
            nb = self._neighbors[node_id]
            nb.last_heard = now
            nb.snr = snr
            nb.rssi = rssi
            nb.packet_count += 1
        else:
            self._neighbors[node_id] = NeighborInfo(
                node_id=node_id,
                last_heard=now,
                snr=snr,
                rssi=rssi,
                packet_count=1,
            )

    def should_accept(self, packet: MeshPacket) -> bool:
        """
        Determine if a received packet should be accepted for processing.

        A packet is accepted if:
        1. It's not a duplicate (not previously seen)
        2. It's addressed to us or is a broadcast

        Note: Even if not addressed to us, we still mark it as seen
        and might rebroadcast it.

        Args:
            packet: The received mesh packet.

        Returns:
            True if the packet should be processed.
        """
        sender = packet.header.source
        pkt_id = packet.header.packet_id

        # Always update neighbor info for the immediate sender
        self._update_neighbor(sender, snr=packet.rx_snr, rssi=packet.rx_rssi)

        # Check for duplicates
        if self._is_duplicate(sender, pkt_id):
            return False

        # Mark as seen
        self._mark_seen(sender, pkt_id, packet.header.hop_limit)

        return True

    def is_for_us(self, packet: MeshPacket) -> bool:
        """
        Check if packet is addressed to this node.

        Args:
            packet: The mesh packet.

        Returns:
            True if addressed to us or is broadcast.
        """
        if self.my_node_id == 0:
            # Observer mode: accept everything
            return True
        dest = packet.header.destination
        return dest == self.my_node_id or dest == BROADCAST_ADDR

    def should_rebroadcast(self, packet: MeshPacket) -> bool:
        """
        Determine if a packet should be rebroadcast (flood routing).

        A packet should be rebroadcast if:
        1. It's not from us
        2. It has hop_limit > 0 (still has hops remaining)
        3. It's a broadcast OR we're acting as a relay
        4. We haven't already rebroadcast it

        Args:
            packet: The received mesh packet.

        Returns:
            True if the packet should be rebroadcast.
        """
        # Don't rebroadcast our own packets
        if packet.header.source == self.my_node_id and self.my_node_id != 0:
            return False

        # No more hops allowed
        if packet.header.hop_limit <= 0:
            return False

        # Only rebroadcast broadcasts (not unicast to others)
        # Unless we're a router node, but concentrators typically don't rebroadcast
        if not packet.header.is_broadcast:
            return False

        return True

    def prepare_rebroadcast(self, packet: MeshPacket) -> Optional[MeshPacket]:
        """
        Prepare a packet for rebroadcast by decrementing hop limit.

        Args:
            packet: The original received packet.

        Returns:
            A new MeshPacket ready for transmission, or None if
            rebroadcast is not appropriate.
        """
        if not self.should_rebroadcast(packet):
            return None

        # Create a new packet with decremented hop limit
        new_header = PacketHeader(
            destination=packet.header.destination,
            source=packet.header.source,  # Keep original source
            packet_id=packet.header.packet_id,  # Keep original packet ID
            hop_limit=packet.header.hop_limit - 1,
            want_ack=packet.header.want_ack,
            channel_hash=packet.header.channel_hash,
        )

        return MeshPacket(
            header=new_header,
            encrypted=packet.encrypted,
            decoded=packet.decoded,
        )

    def get_seen_count(self) -> int:
        """Return number of packets in the seen cache."""
        self._cleanup_seen()
        return len(self._seen)

    def has_seen(self, sender: int, packet_id: int) -> bool:
        """Check if a specific packet has been seen (without marking it)."""
        self._cleanup_seen()
        return (sender, packet_id) in self._seen

    def get_neighbors(self, include_stale: bool = False) -> list[NeighborInfo]:
        """
        Get list of known neighbor nodes.

        Args:
            include_stale: If True, include neighbors not heard recently.

        Returns:
            List of NeighborInfo objects.
        """
        if include_stale:
            return list(self._neighbors.values())
        return [n for n in self._neighbors.values() if not n.is_stale]

    def get_neighbor(self, node_id: int) -> Optional[NeighborInfo]:
        """Get info for a specific neighbor, if known."""
        return self._neighbors.get(node_id)

    def clear_seen(self) -> None:
        """Clear the seen packet cache."""
        self._seen.clear()

    def clear_neighbors(self) -> None:
        """Clear neighbor tracking data."""
        self._neighbors.clear()
