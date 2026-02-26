"""
Meshtastic channel and frequency management.

Defines all Meshtastic LoRa channel presets with their modulation parameters
and US915 frequency plan slot calculations.

Reference: https://meshtastic.org/docs/overview/radio-settings/
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ChannelPreset(Enum):
    """Meshtastic modem preset configurations."""
    LONG_FAST = "LongFast"
    LONG_MODERATE = "LongModerate"
    LONG_SLOW = "LongSlow"
    MEDIUM_FAST = "MediumFast"
    MEDIUM_SLOW = "MediumSlow"
    SHORT_FAST = "ShortFast"
    SHORT_SLOW = "ShortSlow"
    SHORT_TURBO = "ShortTurbo"


@dataclass(frozen=True)
class LoRaModulation:
    """LoRa modulation parameters for a channel preset."""
    preset: ChannelPreset
    bandwidth_hz: int        # Bandwidth in Hz
    spreading_factor: int    # SF (7-12)
    coding_rate: int         # CR denominator (5-8, meaning 4/5 through 4/8)
    preamble_length: int = 16  # Preamble symbols (Meshtastic uses 16 for most)

    @property
    def bandwidth_khz(self) -> float:
        return self.bandwidth_hz / 1000.0

    @property
    def data_rate_name(self) -> str:
        return self.preset.value

    @property
    def coding_rate_str(self) -> str:
        return f"4/{self.coding_rate}"

    def symbol_duration_ms(self) -> float:
        """Calculate symbol duration in milliseconds."""
        return (2 ** self.spreading_factor) / self.bandwidth_hz * 1000.0

    def approx_bitrate_bps(self) -> float:
        """Approximate effective bit rate in bits per second."""
        cr = 4.0 / self.coding_rate
        return self.spreading_factor * (self.bandwidth_hz / (2 ** self.spreading_factor)) * cr


# All Meshtastic channel presets with their LoRa parameters
CHANNEL_PRESETS: dict[ChannelPreset, LoRaModulation] = {
    ChannelPreset.SHORT_TURBO: LoRaModulation(
        preset=ChannelPreset.SHORT_TURBO,
        bandwidth_hz=500_000,
        spreading_factor=7,
        coding_rate=5,
        preamble_length=16,
    ),
    ChannelPreset.SHORT_FAST: LoRaModulation(
        preset=ChannelPreset.SHORT_FAST,
        bandwidth_hz=250_000,
        spreading_factor=7,
        coding_rate=5,
        preamble_length=16,
    ),
    ChannelPreset.SHORT_SLOW: LoRaModulation(
        preset=ChannelPreset.SHORT_SLOW,
        bandwidth_hz=250_000,
        spreading_factor=8,
        coding_rate=5,
        preamble_length=16,
    ),
    ChannelPreset.MEDIUM_FAST: LoRaModulation(
        preset=ChannelPreset.MEDIUM_FAST,
        bandwidth_hz=250_000,
        spreading_factor=9,
        coding_rate=5,
        preamble_length=16,
    ),
    ChannelPreset.MEDIUM_SLOW: LoRaModulation(
        preset=ChannelPreset.MEDIUM_SLOW,
        bandwidth_hz=250_000,
        spreading_factor=10,
        coding_rate=5,
        preamble_length=16,
    ),
    ChannelPreset.LONG_FAST: LoRaModulation(
        preset=ChannelPreset.LONG_FAST,
        bandwidth_hz=250_000,
        spreading_factor=11,
        coding_rate=5,
        preamble_length=16,
    ),
    ChannelPreset.LONG_MODERATE: LoRaModulation(
        preset=ChannelPreset.LONG_MODERATE,
        bandwidth_hz=125_000,
        spreading_factor=11,
        coding_rate=8,
        preamble_length=16,
    ),
    ChannelPreset.LONG_SLOW: LoRaModulation(
        preset=ChannelPreset.LONG_SLOW,
        bandwidth_hz=125_000,
        spreading_factor=12,
        coding_rate=8,
        preamble_length=16,
    ),
}


def get_channel_config(preset: ChannelPreset) -> LoRaModulation:
    """
    Get the LoRa modulation parameters for a channel preset.

    Args:
        preset: The channel preset enum value.

    Returns:
        LoRaModulation with the radio parameters.
    """
    return CHANNEL_PRESETS[preset]


# ---------------------------------------------------------------------------
# US915 frequency plan
# ---------------------------------------------------------------------------

# Meshtastic US915 frequency slot definitions
# Base frequency and number of channels depend on bandwidth
# Reference: RadioInterface.cpp in meshtastic-firmware

# US frequency band: 902-928 MHz
US915_BASE_FREQ_HZ = 902_000_000
US915_END_FREQ_HZ = 928_000_000

# The number of frequency slots depends on bandwidth
# Meshtastic calculates: numChannels = (END - START) / bandwidth
# Then picks a slot using a hash of the channel name

# Default channel frequencies for each preset (US region)
# These are the primary frequencies used by Meshtastic in the US
_US915_DEFAULT_FREQUENCIES: dict[ChannelPreset, int] = {
    # Primary slot for each preset based on default "LongFast" etc naming
    # The actual frequency depends on the channel name hash
    ChannelPreset.LONG_FAST: 906_875_000,
    ChannelPreset.LONG_MODERATE: 906_875_000,
    ChannelPreset.LONG_SLOW: 906_875_000,
    ChannelPreset.MEDIUM_FAST: 906_875_000,
    ChannelPreset.MEDIUM_SLOW: 906_875_000,
    ChannelPreset.SHORT_FAST: 906_875_000,
    ChannelPreset.SHORT_SLOW: 906_875_000,
    ChannelPreset.SHORT_TURBO: 906_875_000,
}


def _channel_name_hash(name: str) -> int:
    """
    Compute channel name hash used for frequency slot selection.

    This matches the Meshtastic firmware's hash function:
    hash = 0, for each byte: hash ^= byte, hash = hash * 31 + byte
    (simplified CRC-like hash).
    """
    h = 0
    for c in name.encode("utf-8"):
        h ^= c
        h = ((h * 31) + c) & 0xFFFFFFFF
    return h


def get_num_slots(bandwidth_hz: int) -> int:
    """
    Calculate the number of frequency slots for given bandwidth in US915.

    Args:
        bandwidth_hz: Channel bandwidth in Hz.

    Returns:
        Number of available frequency slots.
    """
    usable_range = US915_END_FREQ_HZ - US915_BASE_FREQ_HZ
    return usable_range // bandwidth_hz


def get_frequency_hz(
    preset: ChannelPreset,
    channel_name: str = "",
    slot_override: Optional[int] = None,
    region: str = "US",
) -> int:
    """
    Calculate the center frequency in Hz for a given channel configuration.

    Uses the Meshtastic frequency slot algorithm:
      1. Determine number of slots based on bandwidth
      2. Hash channel name to select slot
      3. Calculate center frequency

    Args:
        preset: Channel preset defining bandwidth.
        channel_name: Channel name for slot hashing (default: preset name).
        slot_override: Override the slot index directly.
        region: Region code (currently only "US" supported).

    Returns:
        Center frequency in Hz.

    Raises:
        ValueError: If region is not supported.
    """
    if region != "US":
        raise ValueError(f"Region '{region}' not yet supported, only 'US'")

    config = CHANNEL_PRESETS[preset]
    bw = config.bandwidth_hz
    num_slots = get_num_slots(bw)

    if slot_override is not None:
        slot = slot_override % num_slots
    else:
        name = channel_name if channel_name else preset.value
        slot = _channel_name_hash(name) % num_slots

    # Center frequency = base + (slot * bandwidth) + (bandwidth / 2)
    freq = US915_BASE_FREQ_HZ + (slot * bw) + (bw // 2)
    return freq


def get_all_slot_frequencies(preset: ChannelPreset, region: str = "US") -> list[int]:
    """
    Get all possible frequency slots for a preset.

    Args:
        preset: Channel preset.
        region: Region code.

    Returns:
        List of center frequencies in Hz for all slots.
    """
    config = CHANNEL_PRESETS[preset]
    bw = config.bandwidth_hz
    num_slots = get_num_slots(bw)
    return [US915_BASE_FREQ_HZ + (s * bw) + (bw // 2) for s in range(num_slots)]
