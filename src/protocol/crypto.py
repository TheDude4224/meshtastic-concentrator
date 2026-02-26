"""
Meshtastic encryption/decryption.

Implements AES256-CTR encryption matching the Meshtastic protocol.
See: https://meshtastic.org/docs/overview/encryption

Key derivation:
  - Default key: SHA256("1") = the well-known default Meshtastic key
  - Channel PSK: raw bytes provided by the user (1, 16, or 32 bytes)
  - If PSK is 1 byte, it indexes into a set of well-known keys (legacy)
  - If PSK is 16 bytes, it's expanded to 32 bytes via SHA256
  - If PSK is 32 bytes, used directly as AES-256 key

Nonce construction (128-bit / 16 bytes):
  - Bytes [0:8]:  packet_id as uint64 little-endian (upper 32 bits zero)
  - Bytes [8:12]: source node ID as uint32 little-endian
  - Bytes [12:16]: zero padding
"""

from __future__ import annotations

import hashlib
import struct
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# The well-known default Meshtastic channel key.
# This is SHA256("1") — used when no PSK is configured (the "default" channel).
_DEFAULT_KEY_INPUT = b"1"
DEFAULT_KEY = hashlib.sha256(_DEFAULT_KEY_INPUT).digest()  # 32 bytes


def derive_key(psk: Optional[bytes] = None, channel_name: Optional[str] = None) -> bytes:
    """
    Derive a 32-byte AES key from the provided PSK or channel name.

    Args:
        psk: Pre-shared key bytes. If None, uses the default key.
             - 0 bytes or None: default key (SHA256("1"))
             - 1 byte: legacy simple key expansion (SHA256 of the byte value as string)
             - 16 bytes: expanded to 32 via SHA256
             - 32 bytes: used directly
        channel_name: If provided and psk is None, derive key from channel name.
                      Key = SHA256(channel_name).

    Returns:
        32-byte AES-256 key.

    Raises:
        ValueError: If PSK length is invalid.
    """
    if psk is not None and len(psk) > 0:
        if len(psk) == 1:
            # Legacy: single-byte PSK, expand via SHA256 of the byte value
            return hashlib.sha256(str(psk[0]).encode("ascii")).digest()
        elif len(psk) == 16:
            # 128-bit key, expand to 256-bit via SHA256
            return hashlib.sha256(psk).digest()
        elif len(psk) == 32:
            return psk
        else:
            raise ValueError(
                f"Invalid PSK length {len(psk)} bytes. Must be 0, 1, 16, or 32."
            )

    if channel_name:
        return hashlib.sha256(channel_name.encode("utf-8")).digest()

    return DEFAULT_KEY


def _build_nonce(packet_id: int, source_node_id: int) -> bytes:
    """
    Construct the 16-byte AES-CTR nonce/IV for Meshtastic encryption.

    Format:
      [0:8]   packet_id as little-endian uint64
      [8:12]  source node ID as little-endian uint32
      [12:16] zero padding

    Args:
        packet_id: 32-bit packet identifier.
        source_node_id: 32-bit source node number.

    Returns:
        16-byte nonce.
    """
    return struct.pack("<QI", packet_id & 0xFFFFFFFF, source_node_id & 0xFFFFFFFF) + b"\x00\x00\x00\x00"


class MeshtasticCrypto:
    """
    Handles Meshtastic AES-256-CTR encryption and decryption.

    Usage:
        crypto = MeshtasticCrypto()  # default key
        crypto = MeshtasticCrypto(psk=my_key_bytes)
        plaintext = crypto.decrypt(encrypted_bytes, packet_id, source_node_id)
        ciphertext = crypto.encrypt(plaintext_bytes, packet_id, source_node_id)
    """

    def __init__(
        self,
        psk: Optional[bytes] = None,
        channel_name: Optional[str] = None,
        key: Optional[bytes] = None,
    ) -> None:
        """
        Initialize crypto with a key.

        Args:
            psk: Raw pre-shared key bytes (will be derived/expanded).
            channel_name: Channel name to derive key from (if no psk).
            key: Direct 32-byte AES key (overrides psk/channel_name).
        """
        if key is not None:
            if len(key) != 32:
                raise ValueError(f"Direct key must be 32 bytes, got {len(key)}")
            self._key = key
        else:
            self._key = derive_key(psk=psk, channel_name=channel_name)

    @property
    def key(self) -> bytes:
        """The 32-byte AES key."""
        return self._key

    def encrypt(self, plaintext: bytes, packet_id: int, source_node_id: int) -> bytes:
        """
        Encrypt plaintext using AES-256-CTR.

        Args:
            plaintext: Data protobuf bytes to encrypt.
            packet_id: Packet ID (used in nonce).
            source_node_id: Source node number (used in nonce).

        Returns:
            Encrypted bytes (same length as plaintext).
        """
        nonce = _build_nonce(packet_id, source_node_id)
        cipher = Cipher(algorithms.AES(self._key), modes.CTR(nonce))
        encryptor = cipher.encryptor()
        return encryptor.update(plaintext) + encryptor.finalize()

    def decrypt(self, ciphertext: bytes, packet_id: int, source_node_id: int) -> bytes:
        """
        Decrypt ciphertext using AES-256-CTR.

        AES-CTR is symmetric, so decrypt == encrypt with same parameters.

        Args:
            ciphertext: Encrypted payload bytes.
            packet_id: Packet ID from the packet header.
            source_node_id: Source node ID from the packet header.

        Returns:
            Decrypted bytes (Data protobuf).
        """
        # CTR mode: encryption and decryption are identical operations
        return self.encrypt(ciphertext, packet_id, source_node_id)

    @staticmethod
    def channel_hash(channel_name: str, key: bytes) -> int:
        """
        Compute the 4-bit channel hash used in the packet header.

        The channel hash is XOR of all key bytes, XORed with XOR of channel name bytes,
        then masked to 4 bits.

        Args:
            channel_name: Channel name string.
            key: The full 32-byte AES key.

        Returns:
            4-bit channel hash (0-15).
        """
        h = 0
        for b in key:
            h ^= b
        for b in channel_name.encode("utf-8"):
            h ^= b
        return h & 0x0F
