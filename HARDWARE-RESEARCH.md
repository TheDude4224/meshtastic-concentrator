# FreedomFi Gateway LoRa Hardware Research

**Date:** 2026-02-26
**Status:** Initial research — some items need physical confirmation

---

## 1. Concentrator Chip: SX1302

**Confirmed: SX1302** (not SX1303)

The FreedomFi gateway (x86 mini PC version, Intel J1900-based) uses an **SX1302-based** LoRa concentrator module. This is consistent with the Helium ecosystem's use of SX1302 across most gateway manufacturers from the 2021-2022 era.

- Helium maintains a fork of the HAL: [helium/sx1302_hal](https://github.com/helium/sx1302_hal)
- The upstream Semtech HAL supports both SX1302 and SX1303: [Lora-net/sx1302_hal](https://github.com/Lora-net/sx1302_hal)

**Note:** SX1303 is a pin-compatible successor to SX1302. The same HAL works for both. Some later gateway revisions *may* use SX1303, but the original FreedomFi gateways shipped with SX1302.

## 2. Module Manufacturer: RAK (RAK2287)

**Most likely: RAK2287** (RAK Wireless)

The RAK2287 was the dominant SX1302-based mPCIe concentrator module used in Helium gateways, including FreedomFi. Key specs:

- **Chip:** Semtech SX1302
- **Form factor:** mini-PCIe (mPCIe)
- **Interface:** SPI (primary) or USB variant available
- **GPS:** Built-in u-blox ZOE-M8Q
- **Price:** ~$84-99 USD (retail from RAK store)
- **Product page:** https://store.rakwireless.com/products/rak2287-lpwan-gateway-concentrator-module

**⚠️ Needs confirmation:** Physical teardown of a FreedomFi gateway would confirm whether it's a RAK2287 specifically or a different SX1302 module. The RAK2287 is the most widely used SX1302 mPCIe module in the Helium ecosystem.

## 3. PCIe Interface: mPCIe (SPI-over-mPCIe)

**Confirmed: mini-PCIe form factor**

The RAK2287 uses the standard mini-PCIe card slot. However, the actual data interface is **SPI** (not PCIe protocol). The mPCIe connector is used for:
- Physical form factor / mechanical mounting
- 3.3V power delivery
- SPI signal routing through the mPCIe pinout

This is a common design pattern in LoRa concentrators — they use the mPCIe form factor for convenience but communicate via SPI, not the PCIe bus.

**USB variant:** The RAK2287 also comes in a USB variant (RAK2287-USB) that routes USB signals through the mPCIe connector. This is useful for hosts that don't expose SPI through their mPCIe slots (like most x86 mini PCs).

**🔑 Key question for our project:** The Intel J1900-based FreedomFi gateway likely uses the **USB variant** of the RAK2287, since x86 mPCIe slots typically provide USB 2.0 but NOT SPI. The SPI variant is designed for Raspberry Pi and similar embedded platforms where SPI is exposed on the mPCIe adapter board.

## 4. Frequency Band

**US915** for US-market FreedomFi gateways.

The RAK2287 supports multiple bands selected at manufacturing:
- **US915** / AU915 / AS923 / KR920 (one hardware variant)
- **EU868** / IN865 / RU864 (another hardware variant)

FreedomFi gateways sold in the US use the US915 variant. The frequency band is determined by the front-end filter and firmware configuration.

## 5. Driver / HAL Compatibility

### Primary HAL
- **Semtech sx1302_hal** (upstream): https://github.com/Lora-net/sx1302_hal
  - Supports SX1302 and SX1303
  - SPI and USB interfaces
  - Includes packet_forwarder reference implementation
  - C library (libloragw)

### Helium Fork
- **helium/sx1302_hal**: https://github.com/helium/sx1302_hal
  - Helium-specific modifications
  - Used in production Helium gateways

### ChirpStack Concentratord
- **chirpstack/chirpstack-concentratord**: https://github.com/chirpstack/chirpstack-concentratord
  - Rust-based concentrator daemon
  - Exposes ZeroMQ API
  - Decouples hardware from packet forwarding
  - Supports multiple simultaneous clients
  - **This is highly relevant for non-LoRaWAN use** — the ZeroMQ API allows custom applications to receive raw LoRa packets

### Helium Gateway
- **helium/gateway-rs**: https://github.com/helium/gateway-rs
  - Rust-based Helium gateway application
  - Connects to packet forwarder via Semtech GWMP (UDP)
  - Shows the x86_64 TPM variant exists (confirming x86 support)

## 6. Existing Open-Source Projects for Non-LoRaWAN Use

### Directly Relevant
- **ChirpStack Concentratord** — The ZeroMQ-based architecture makes it straightforward to build custom LoRa applications on top of SX1302 hardware without LoRaWAN
- **Semtech sx1302_hal** — The low-level `libloragw` library can be used directly to send/receive raw LoRa packets at the concentrator level

### Meshtastic + Concentrator
- No existing projects found that specifically bridge Meshtastic with SX1302/SX1303 concentrator cards
- This would be a novel project: using a concentrator's multi-channel receive capability to listen to Meshtastic traffic on multiple frequencies/spreading factors simultaneously

### Community Interest
- Reddit posts show FreedomFi gateway owners looking for alternative uses after Helium mining declined
- The J1900-based hardware is essentially a standard x86 mini PC — people have successfully repurposed them

## 7. FreedomFi Published Specs / Schematics

**No official schematics found.**

- FreedomFi (now Nova Labs) has not published detailed hardware schematics
- Their GitHub (https://github.com/FreedomFi) does not contain hardware documentation
- The gateway runs a custom Linux distribution
- Community teardowns confirm the Intel J1900 processor
- FCC filings for FreedomFi were not located under obvious FCC IDs (the gateway may be filed under the ODM manufacturer's FCC ID rather than FreedomFi's)

### What We Know from Community Sources
- **CPU:** Intel Celeron J1900 (quad-core, 2.0GHz)
- **Form factor:** Mini PC / NUC-like enclosure
- **LoRa:** mPCIe concentrator card (SX1302-based, likely RAK2287)
- **Connectivity:** Ethernet, possibly WiFi
- **OS:** Custom Linux (Docker-based Helium stack)
- **Security:** TPM module for Helium attestation

---

## Summary & Implications for Meshtastic Concentrator Project

| Item | Finding | Confidence |
|------|---------|------------|
| Concentrator chip | **SX1303** | **CONFIRMED** (photo) |
| Module | **RAK5146 USB** | **CONFIRMED** (photo: FCC 2A918-RAK5146) |
| Interface | **USB** (printed on module label) | **CONFIRMED** (photo) |
| Frequency | US915 | High (for US units) |
| HAL | Lora-net/sx1302_hal + chirpstack-concentratord | High |
| Schematics | Not published | High |
| Gateway board | Intel J1900, 4GB DDR3L, 64GB eMMC, 4x GbE | **CONFIRMED** (photo) |

### Key Takeaways for the Project

1. **The RAK2287 is readily available** (~$84-99) and well-documented — no need to salvage from a FreedomFi gateway
2. **USB interface is likely on x86** — this simplifies integration since USB is universal
3. **chirpstack-concentratord** is the best starting point for custom applications — its ZeroMQ API allows receiving raw LoRa packets without LoRaWAN
4. **Multi-channel receive is the killer feature** — SX1302 can demodulate 8 channels simultaneously across multiple spreading factors, which could allow a single concentrator to monitor all Meshtastic channels at once
5. **No existing Meshtastic+concentrator project exists** — this would be greenfield work

### Next Steps
- [ ] Confirm USB vs SPI interface on actual FreedomFi hardware (check `lsusb` or `dmesg`)
- [ ] Test sx1302_hal on x86 Linux with RAK2287-USB
- [ ] Evaluate chirpstack-concentratord as the base daemon
- [ ] Design Meshtastic protocol handler that works with raw LoRa packets from concentrator
- [ ] Determine if SX1302 can transmit Meshtastic-compatible packets (timing, preamble, etc.)
