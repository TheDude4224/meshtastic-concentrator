"""
Unit tests for the Meshtastic protocol implementation.
"""

import json
import struct
import tempfile
import time
from pathlib import Path

import pytest

from src.protocol.packets import (
    Data,
    MeshPacket,
    PacketHeader,
    PortNum,
    Position,
    User,
    BROADCAST_ADDR,
    HEADER_SIZE,
    encode_mesh_packet,
    decode_mesh_packet,
    decode_position,
    decode_nodeinfo,
    _encode_varint,
    _decode_varint,
)
from src.protocol.crypto import MeshtasticCrypto, DEFAULT_KEY, derive_key, _build_nonce
from src.protocol.channels import (
    ChannelPreset,
    LoRaModulation,
    get_channel_config,
    get_frequency_hz,
    get_num_slots,
    get_all_slot_frequencies,
    CHANNEL_PRESETS,
    US915_BASE_FREQ_HZ,
    US915_END_FREQ_HZ,
)
from src.protocol.routing import MeshRouter
from src.protocol.nodedb import NodeDB, NodeInfo, NodePosition


# ===========================================================================
# Protobuf encoding helpers
# ===========================================================================

class TestVarint:
    def test_encode_zero(self):
        assert _encode_varint(0) == b"\x00"

    def test_encode_small(self):
        assert _encode_varint(1) == b"\x01"
        assert _encode_varint(127) == b"\x7f"

    def test_encode_multibyte(self):
        assert _encode_varint(128) == b"\x80\x01"
        assert _encode_varint(300) == b"\xac\x02"

    def test_round_trip(self):
        for val in [0, 1, 127, 128, 255, 300, 16384, 0xFFFFFFFF]:
            encoded = _encode_varint(val)
            decoded, offset = _decode_varint(encoded, 0)
            assert decoded == val
            assert offset == len(encoded)


# ===========================================================================
# Packet encoding/decoding
# ===========================================================================

class TestPacketHeader:
    def test_encode_decode_roundtrip(self):
        hdr = PacketHeader(
            destination=BROADCAST_ADDR,
            source=0xAABBCCDD,
            packet_id=0x12345678,
            hop_limit=3,
            want_ack=True,
            channel_hash=5,
        )
        raw = hdr.encode()
        assert len(raw) == HEADER_SIZE

        decoded = PacketHeader.decode(raw)
        assert decoded.destination == BROADCAST_ADDR
        assert decoded.source == 0xAABBCCDD
        assert decoded.packet_id == 0x12345678
        assert decoded.hop_limit == 3
        assert decoded.want_ack is True
        assert decoded.channel_hash == 5

    def test_flags_byte(self):
        hdr = PacketHeader(hop_limit=5, want_ack=False, channel_hash=0xA)
        assert hdr.flags == (5 | (0xA << 4))

        hdr2 = PacketHeader()
        hdr2.flags = 0b10101011  # hop=3, want_ack=1, channel=0xA
        assert hdr2.hop_limit == 3
        assert hdr2.want_ack is True
        assert hdr2.channel_hash == 0xA

    def test_broadcast_detection(self):
        assert PacketHeader(destination=BROADCAST_ADDR).is_broadcast
        assert not PacketHeader(destination=0x12345678).is_broadcast

    def test_too_short_raises(self):
        with pytest.raises(ValueError):
            PacketHeader.decode(b"\x00" * 5)


class TestDataMessage:
    def test_encode_decode_text(self):
        data = Data(
            portnum=PortNum.TEXT_MESSAGE_APP,
            payload=b"Hello Mesh!",
            want_response=False,
        )
        encoded = data.encode()
        decoded = Data.decode(encoded)
        assert decoded.portnum == PortNum.TEXT_MESSAGE_APP
        assert decoded.payload == b"Hello Mesh!"
        assert decoded.get_text() == "Hello Mesh!"

    def test_encode_decode_with_all_fields(self):
        data = Data(
            portnum=PortNum.POSITION_APP,
            payload=b"\x01\x02\x03",
            want_response=True,
            dest=0x11111111,
            source=0x22222222,
            request_id=0x33333333,
            reply_id=0x44444444,
            emoji=0x55555555,
            bitfield=7,
        )
        encoded = data.encode()
        decoded = Data.decode(encoded)
        assert decoded.portnum == PortNum.POSITION_APP
        assert decoded.payload == b"\x01\x02\x03"
        assert decoded.want_response is True
        assert decoded.dest == 0x11111111
        assert decoded.source == 0x22222222
        assert decoded.request_id == 0x33333333
        assert decoded.reply_id == 0x44444444
        assert decoded.emoji == 0x55555555
        assert decoded.bitfield == 7

    def test_get_text_non_text(self):
        data = Data(portnum=PortNum.POSITION_APP, payload=b"\x00\x01")
        assert data.get_text() is None


class TestPosition:
    def test_encode_decode_roundtrip(self):
        pos = Position(
            latitude_i=421234567,
            longitude_i=-839876543,
            altitude=300,
            time=1700000000,
            sats_in_view=12,
        )
        encoded = pos.encode()
        decoded = Position.decode(encoded)
        assert decoded.latitude_i == 421234567
        assert decoded.longitude_i == -839876543
        assert decoded.altitude == 300
        assert decoded.time == 1700000000
        assert decoded.sats_in_view == 12

    def test_lat_lon_properties(self):
        pos = Position(latitude_i=421234567, longitude_i=-839876543)
        assert abs(pos.latitude - 42.1234567) < 1e-10
        assert abs(pos.longitude - (-83.9876543)) < 1e-10

    def test_none_position(self):
        pos = Position()
        assert pos.latitude is None
        assert pos.longitude is None


class TestUser:
    def test_encode_decode_roundtrip(self):
        user = User(
            id="!aabbccdd",
            long_name="Test Node",
            short_name="TN",
            hw_model=43,  # HELTEC_V3
        )
        encoded = user.encode()
        decoded = User.decode(encoded)
        assert decoded.id == "!aabbccdd"
        assert decoded.long_name == "Test Node"
        assert decoded.short_name == "TN"
        assert decoded.hw_model == 43


class TestMeshPacket:
    def test_over_the_air_roundtrip(self):
        header = PacketHeader(
            destination=BROADCAST_ADDR,
            source=0xAABBCCDD,
            packet_id=0x12345678,
            hop_limit=3,
            want_ack=False,
            channel_hash=8,
        )
        payload = b"\x01\x02\x03\x04\x05"
        raw = encode_mesh_packet(header, payload)
        assert len(raw) == HEADER_SIZE + len(payload)

        pkt = decode_mesh_packet(raw)
        assert pkt.header.destination == BROADCAST_ADDR
        assert pkt.header.source == 0xAABBCCDD
        assert pkt.header.packet_id == 0x12345678
        assert pkt.header.hop_limit == 3
        assert pkt.encrypted == payload

    def test_protobuf_roundtrip(self):
        data = Data(portnum=PortNum.TEXT_MESSAGE_APP, payload=b"test")
        pkt = MeshPacket(
            header=PacketHeader(
                source=0x11111111,
                destination=BROADCAST_ADDR,
                packet_id=42,
                hop_limit=3,
            ),
            decoded=data,
        )
        pb = pkt.to_protobuf()
        decoded_pkt = MeshPacket.from_protobuf(pb)
        assert decoded_pkt.header.source == 0x11111111
        assert decoded_pkt.header.destination == BROADCAST_ADDR
        assert decoded_pkt.header.packet_id == 42
        assert decoded_pkt.decoded is not None
        assert decoded_pkt.decoded.portnum == PortNum.TEXT_MESSAGE_APP
        assert decoded_pkt.decoded.payload == b"test"

    def test_decode_too_short(self):
        with pytest.raises(ValueError):
            decode_mesh_packet(b"\x00" * 5)


# ===========================================================================
# Crypto
# ===========================================================================

class TestCrypto:
    def test_default_key(self):
        """Default key should be SHA256('1')."""
        import hashlib
        expected = hashlib.sha256(b"1").digest()
        assert DEFAULT_KEY == expected
        assert len(DEFAULT_KEY) == 32

    def test_encrypt_decrypt_roundtrip(self):
        crypto = MeshtasticCrypto()
        plaintext = b"Hello, Meshtastic world!"
        packet_id = 0x12345678
        source_id = 0xAABBCCDD

        ciphertext = crypto.encrypt(plaintext, packet_id, source_id)
        assert ciphertext != plaintext
        assert len(ciphertext) == len(plaintext)

        decrypted = crypto.decrypt(ciphertext, packet_id, source_id)
        assert decrypted == plaintext

    def test_different_keys_produce_different_ciphertext(self):
        crypto1 = MeshtasticCrypto()
        crypto2 = MeshtasticCrypto(psk=b"A" * 32)
        plaintext = b"secret message"
        ct1 = crypto1.encrypt(plaintext, 1, 1)
        ct2 = crypto2.encrypt(plaintext, 1, 1)
        assert ct1 != ct2

    def test_different_nonces_produce_different_ciphertext(self):
        crypto = MeshtasticCrypto()
        plaintext = b"test data"
        ct1 = crypto.encrypt(plaintext, packet_id=1, source_node_id=1)
        ct2 = crypto.encrypt(plaintext, packet_id=2, source_node_id=1)
        ct3 = crypto.encrypt(plaintext, packet_id=1, source_node_id=2)
        assert ct1 != ct2
        assert ct1 != ct3

    def test_nonce_structure(self):
        nonce = _build_nonce(0x12345678, 0xAABBCCDD)
        assert len(nonce) == 16
        # packet_id as uint64 LE
        assert struct.unpack("<Q", nonce[:8])[0] == 0x12345678
        # source node as uint32 LE
        assert struct.unpack("<I", nonce[8:12])[0] == 0xAABBCCDD
        # trailing zeros
        assert nonce[12:16] == b"\x00\x00\x00\x00"

    def test_custom_psk_32_bytes(self):
        key = bytes(range(32))
        crypto = MeshtasticCrypto(psk=key)
        assert crypto.key == key

    def test_custom_psk_16_bytes_expanded(self):
        psk16 = bytes(range(16))
        crypto = MeshtasticCrypto(psk=psk16)
        assert len(crypto.key) == 32
        assert crypto.key != psk16  # expanded

    def test_custom_psk_1_byte(self):
        crypto = MeshtasticCrypto(psk=b"\x05")
        assert len(crypto.key) == 32

    def test_invalid_psk_length(self):
        with pytest.raises(ValueError):
            MeshtasticCrypto(psk=b"\x00" * 7)

    def test_channel_name_key(self):
        crypto = MeshtasticCrypto(channel_name="MyChannel")
        assert len(crypto.key) == 32
        # Same channel name should produce same key
        crypto2 = MeshtasticCrypto(channel_name="MyChannel")
        assert crypto.key == crypto2.key

    def test_direct_key(self):
        key = b"\xAA" * 32
        crypto = MeshtasticCrypto(key=key)
        assert crypto.key == key

    def test_channel_hash(self):
        h = MeshtasticCrypto.channel_hash("LongFast", DEFAULT_KEY)
        assert 0 <= h <= 15

    def test_full_packet_encrypt_decrypt(self):
        """Test encrypting/decrypting a full Data protobuf payload."""
        crypto = MeshtasticCrypto()
        data = Data(portnum=PortNum.TEXT_MESSAGE_APP, payload=b"Hello mesh!")
        plaintext = data.encode()

        pkt_id = 99
        src_id = 0x12345678

        encrypted = crypto.encrypt(plaintext, pkt_id, src_id)
        decrypted = crypto.decrypt(encrypted, pkt_id, src_id)
        assert decrypted == plaintext

        # Parse the decrypted bytes back
        decoded = Data.decode(decrypted)
        assert decoded.portnum == PortNum.TEXT_MESSAGE_APP
        assert decoded.payload == b"Hello mesh!"


# ===========================================================================
# Channels
# ===========================================================================

class TestChannels:
    def test_all_presets_defined(self):
        for preset in ChannelPreset:
            config = get_channel_config(preset)
            assert isinstance(config, LoRaModulation)
            assert config.bandwidth_hz > 0
            assert 7 <= config.spreading_factor <= 12
            assert 5 <= config.coding_rate <= 8

    def test_long_fast_params(self):
        config = get_channel_config(ChannelPreset.LONG_FAST)
        assert config.bandwidth_hz == 250_000
        assert config.spreading_factor == 11
        assert config.coding_rate == 5

    def test_long_slow_params(self):
        config = get_channel_config(ChannelPreset.LONG_SLOW)
        assert config.bandwidth_hz == 125_000
        assert config.spreading_factor == 12
        assert config.coding_rate == 8

    def test_short_turbo_params(self):
        config = get_channel_config(ChannelPreset.SHORT_TURBO)
        assert config.bandwidth_hz == 500_000
        assert config.spreading_factor == 7

    def test_frequency_in_band(self):
        for preset in ChannelPreset:
            freq = get_frequency_hz(preset)
            assert US915_BASE_FREQ_HZ <= freq <= US915_END_FREQ_HZ, (
                f"{preset.value}: {freq} Hz out of US915 band"
            )

    def test_frequency_deterministic(self):
        f1 = get_frequency_hz(ChannelPreset.LONG_FAST, "LongFast")
        f2 = get_frequency_hz(ChannelPreset.LONG_FAST, "LongFast")
        assert f1 == f2

    def test_different_channels_can_differ(self):
        f1 = get_frequency_hz(ChannelPreset.LONG_FAST, "Channel1")
        f2 = get_frequency_hz(ChannelPreset.LONG_FAST, "Channel2")
        # Different names CAN produce different frequencies (not guaranteed but likely)
        # Just verify they're both valid
        assert US915_BASE_FREQ_HZ <= f1 <= US915_END_FREQ_HZ
        assert US915_BASE_FREQ_HZ <= f2 <= US915_END_FREQ_HZ

    def test_slot_override(self):
        freq = get_frequency_hz(ChannelPreset.LONG_FAST, slot_override=0)
        config = get_channel_config(ChannelPreset.LONG_FAST)
        expected = US915_BASE_FREQ_HZ + (config.bandwidth_hz // 2)
        assert freq == expected

    def test_num_slots(self):
        # 26 MHz range / 250 kHz = 104 slots
        assert get_num_slots(250_000) == 104
        # 26 MHz range / 125 kHz = 208 slots
        assert get_num_slots(125_000) == 208
        # 26 MHz range / 500 kHz = 52 slots
        assert get_num_slots(500_000) == 52

    def test_all_slot_frequencies(self):
        freqs = get_all_slot_frequencies(ChannelPreset.LONG_FAST)
        assert len(freqs) == 104
        # All should be in band
        for f in freqs:
            assert US915_BASE_FREQ_HZ <= f <= US915_END_FREQ_HZ
        # Should be monotonically increasing
        assert freqs == sorted(freqs)

    def test_unsupported_region(self):
        with pytest.raises(ValueError):
            get_frequency_hz(ChannelPreset.LONG_FAST, region="EU868")

    def test_symbol_duration(self):
        config = get_channel_config(ChannelPreset.LONG_FAST)
        dur = config.symbol_duration_ms()
        assert dur > 0

    def test_coding_rate_str(self):
        config = get_channel_config(ChannelPreset.LONG_FAST)
        assert config.coding_rate_str == "4/5"
        config2 = get_channel_config(ChannelPreset.LONG_SLOW)
        assert config2.coding_rate_str == "4/8"


# ===========================================================================
# Routing
# ===========================================================================

class TestRouting:
    def _make_packet(
        self,
        source: int = 0xAAAAAAAA,
        dest: int = BROADCAST_ADDR,
        pkt_id: int = 1,
        hop_limit: int = 3,
    ) -> MeshPacket:
        return MeshPacket(
            header=PacketHeader(
                destination=dest,
                source=source,
                packet_id=pkt_id,
                hop_limit=hop_limit,
            ),
            encrypted=b"\x00",
        )

    def test_accept_new_packet(self):
        router = MeshRouter(my_node_id=0xBBBBBBBB)
        pkt = self._make_packet()
        assert router.should_accept(pkt) is True

    def test_reject_duplicate(self):
        router = MeshRouter()
        pkt = self._make_packet()
        assert router.should_accept(pkt) is True
        assert router.should_accept(pkt) is False

    def test_different_packets_accepted(self):
        router = MeshRouter()
        pkt1 = self._make_packet(pkt_id=1)
        pkt2 = self._make_packet(pkt_id=2)
        assert router.should_accept(pkt1) is True
        assert router.should_accept(pkt2) is True

    def test_same_id_different_sender(self):
        router = MeshRouter()
        pkt1 = self._make_packet(source=1, pkt_id=1)
        pkt2 = self._make_packet(source=2, pkt_id=1)
        assert router.should_accept(pkt1) is True
        assert router.should_accept(pkt2) is True

    def test_should_rebroadcast(self):
        router = MeshRouter(my_node_id=0xBBBBBBBB)
        pkt = self._make_packet(hop_limit=3)
        assert router.should_rebroadcast(pkt) is True

    def test_no_rebroadcast_zero_hops(self):
        router = MeshRouter()
        pkt = self._make_packet(hop_limit=0)
        assert router.should_rebroadcast(pkt) is False

    def test_no_rebroadcast_own_packet(self):
        router = MeshRouter(my_node_id=0xAAAAAAAA)
        pkt = self._make_packet(source=0xAAAAAAAA)
        assert router.should_rebroadcast(pkt) is False

    def test_no_rebroadcast_unicast(self):
        router = MeshRouter(my_node_id=0xBBBBBBBB)
        pkt = self._make_packet(dest=0xCCCCCCCC)
        assert router.should_rebroadcast(pkt) is False

    def test_prepare_rebroadcast_decrements_hop(self):
        router = MeshRouter(my_node_id=0xBBBBBBBB)
        pkt = self._make_packet(hop_limit=5)
        rebroadcast = router.prepare_rebroadcast(pkt)
        assert rebroadcast is not None
        assert rebroadcast.header.hop_limit == 4
        assert rebroadcast.header.source == pkt.header.source
        assert rebroadcast.header.packet_id == pkt.header.packet_id

    def test_neighbor_tracking(self):
        router = MeshRouter()
        pkt = self._make_packet(source=0x11111111)
        pkt.rx_snr = 10.5
        pkt.rx_rssi = -80
        router.should_accept(pkt)

        neighbors = router.get_neighbors()
        assert len(neighbors) == 1
        assert neighbors[0].node_id == 0x11111111
        assert neighbors[0].snr == 10.5

    def test_seen_count(self):
        router = MeshRouter()
        for i in range(10):
            pkt = self._make_packet(pkt_id=i)
            router.should_accept(pkt)
        assert router.get_seen_count() == 10

    def test_has_seen(self):
        router = MeshRouter()
        pkt = self._make_packet(source=0xAAAAAAAA, pkt_id=42)
        router.should_accept(pkt)
        assert router.has_seen(0xAAAAAAAA, 42) is True
        assert router.has_seen(0xAAAAAAAA, 99) is False

    def test_is_for_us(self):
        router = MeshRouter(my_node_id=0xBBBBBBBB)
        assert router.is_for_us(self._make_packet(dest=0xBBBBBBBB))
        assert router.is_for_us(self._make_packet(dest=BROADCAST_ADDR))
        assert not router.is_for_us(self._make_packet(dest=0xCCCCCCCC))

    def test_observer_mode(self):
        router = MeshRouter(my_node_id=0)
        assert router.is_for_us(self._make_packet(dest=0xCCCCCCCC))


# ===========================================================================
# Node Database
# ===========================================================================

class TestNodeDB:
    def test_basic_tracking(self):
        db = NodeDB()
        pkt = MeshPacket(
            header=PacketHeader(source=0x11111111, destination=BROADCAST_ADDR, packet_id=1),
        )
        pkt.rx_snr = 5.0
        pkt.rx_rssi = -90

        node = db.update_from_packet(pkt)
        assert node is not None
        assert node.node_id == 0x11111111
        assert node.snr == 5.0
        assert node.rssi == -90
        assert node.packet_count == 1

    def test_update_increments_count(self):
        db = NodeDB()
        for i in range(5):
            pkt = MeshPacket(
                header=PacketHeader(source=0x11111111, destination=BROADCAST_ADDR, packet_id=i),
            )
            db.update_from_packet(pkt)
        assert db.get_node(0x11111111).packet_count == 5

    def test_position_update(self):
        db = NodeDB()
        pos = Position(latitude_i=421234567, longitude_i=-839876543, altitude=300)
        data = Data(portnum=PortNum.POSITION_APP, payload=pos.encode())
        pkt = MeshPacket(
            header=PacketHeader(source=0x22222222, destination=BROADCAST_ADDR, packet_id=1),
            decoded=data,
        )
        node = db.update_from_packet(pkt)
        assert node.position is not None
        assert node.position.latitude_i == 421234567
        assert abs(node.position.latitude - 42.1234567) < 1e-10

    def test_nodeinfo_update(self):
        db = NodeDB()
        user = User(id="!aabbccdd", long_name="Test Node", short_name="TN")
        data = Data(portnum=PortNum.NODEINFO_APP, payload=user.encode())
        pkt = MeshPacket(
            header=PacketHeader(source=0xAABBCCDD, destination=BROADCAST_ADDR, packet_id=1),
            decoded=data,
        )
        node = db.update_from_packet(pkt)
        assert node.long_name == "Test Node"
        assert node.short_name == "TN"
        assert node.display_name == "Test Node"

    def test_json_export_import(self):
        db = NodeDB()
        # Add some nodes
        for i in range(3):
            pkt = MeshPacket(
                header=PacketHeader(source=0x10000000 + i, destination=BROADCAST_ADDR, packet_id=i),
            )
            db.update_from_packet(pkt)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name

        db.export_json(path)

        # Import into a new db
        db2 = NodeDB()
        count = db2.import_json(path)
        assert count == 3
        assert len(db2) == 3

        Path(path).unlink()

    def test_expire_stale(self):
        db = NodeDB(expiry_seconds=1)
        pkt = MeshPacket(
            header=PacketHeader(source=0x11111111, destination=BROADCAST_ADDR, packet_id=1),
        )
        node = db.update_from_packet(pkt)
        # Manually set last_heard to the past
        node.last_heard = time.time() - 10

        expired = db.expire_stale()
        assert 0x11111111 in expired
        assert len(db) == 0

    def test_contains_and_len(self):
        db = NodeDB()
        assert len(db) == 0
        assert 0x11111111 not in db
        db.get_or_create(0x11111111)
        assert len(db) == 1
        assert 0x11111111 in db

    def test_ignore_broadcast_source(self):
        db = NodeDB()
        pkt = MeshPacket(
            header=PacketHeader(source=BROADCAST_ADDR, destination=BROADCAST_ADDR, packet_id=1),
        )
        assert db.update_from_packet(pkt) is None
        assert len(db) == 0

    def test_summary(self):
        db = NodeDB()
        s = db.summary()
        assert "NodeDB" in s
        assert "0 nodes" in s

    def test_node_id_hex(self):
        node = NodeInfo(node_id=0xAABBCCDD)
        assert node.node_id_hex == "!aabbccdd"

    def test_get_nodes_with_position(self):
        db = NodeDB()
        n1 = db.get_or_create(1)
        n1.position = NodePosition(latitude_i=100, longitude_i=200)
        n2 = db.get_or_create(2)
        # n2 has no position
        assert len(db.get_nodes_with_position()) == 1


# ===========================================================================
# Integration: full packet lifecycle
# ===========================================================================

class TestIntegration:
    def test_full_send_receive_cycle(self):
        """Simulate encoding, encrypting, decrypting, and decoding a text message."""
        crypto = MeshtasticCrypto()  # default key

        # Sender creates a text message
        data = Data(portnum=PortNum.TEXT_MESSAGE_APP, payload=b"Hello from sender!")
        plaintext = data.encode()

        # Create packet header
        header = PacketHeader(
            destination=BROADCAST_ADDR,
            source=0xAABBCCDD,
            packet_id=0x00000042,
            hop_limit=3,
            want_ack=False,
            channel_hash=8,
        )

        # Encrypt
        encrypted = crypto.encrypt(plaintext, header.packet_id, header.source)

        # Build over-the-air packet
        raw = encode_mesh_packet(header, encrypted)

        # --- Receiver side ---
        # Decode header
        received = decode_mesh_packet(raw)
        assert received.header.source == 0xAABBCCDD
        assert received.header.destination == BROADCAST_ADDR

        # Decrypt
        decrypted = crypto.decrypt(
            received.encrypted,
            received.header.packet_id,
            received.header.source,
        )

        # Parse Data
        received_data = Data.decode(decrypted)
        assert received_data.portnum == PortNum.TEXT_MESSAGE_APP
        assert received_data.get_text() == "Hello from sender!"

    def test_router_and_nodedb_integration(self):
        """Test routing + node tracking working together."""
        router = MeshRouter(my_node_id=0xBBBBBBBB)
        db = NodeDB()
        crypto = MeshtasticCrypto()

        # Simulate receiving a position packet
        pos = Position(latitude_i=421234567, longitude_i=-839876543)
        data = Data(portnum=PortNum.POSITION_APP, payload=pos.encode())
        plaintext = data.encode()

        header = PacketHeader(
            destination=BROADCAST_ADDR,
            source=0x11111111,
            packet_id=100,
            hop_limit=2,
        )
        encrypted = crypto.encrypt(plaintext, header.packet_id, header.source)
        raw = encode_mesh_packet(header, encrypted)

        # Receive
        pkt = decode_mesh_packet(raw)
        pkt.rx_snr = 8.5
        pkt.rx_rssi = -75

        # Route
        assert router.should_accept(pkt)
        assert router.is_for_us(pkt)

        # Decrypt and decode
        decrypted = crypto.decrypt(pkt.encrypted, pkt.header.packet_id, pkt.header.source)
        pkt.decoded = Data.decode(decrypted)

        # Update node DB
        node = db.update_from_packet(pkt)
        assert node is not None
        assert node.position is not None
        assert abs(node.position.latitude - 42.1234567) < 1e-10

        # Verify router would rebroadcast
        assert router.should_rebroadcast(pkt)
        rb = router.prepare_rebroadcast(pkt)
        assert rb.header.hop_limit == 1
