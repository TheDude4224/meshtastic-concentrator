#!/usr/bin/env bash
# =============================================================================
# Build chirpstack-concentratord-sx1302 from source
# Required because: no working apt package exists for this distro/version
#
# Tested on: Ubuntu 24.04 (Noble), Intel J1900, 2026-03-01
# Build time: ~5 min on J1900, ~1 min on modern machine
# Output: /usr/local/bin/chirpstack-concentratord-sx1302
# =============================================================================
set -euo pipefail

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

BROCAAR_HAL_TAG="V2.1.0r9"   # Must be brocaar's fork, NOT official Lora-net/sx1302_hal
CONCENTRATORD_DIR="${HOME}/chirpstack-concentratord"

# ---- Dependencies ----
log "Installing build dependencies..."
sudo apt-get install -y \
  build-essential git curl \
  protobuf-compiler libprotobuf-dev \
  clang libclang-dev \
  pkg-config

# ---- Rust ----
if ! command -v cargo &>/dev/null; then
  log "Installing Rust toolchain..."
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path
fi
source "$HOME/.cargo/env"
log "Rust: $(rustc --version)"

# ---- brocaar's sx1302_hal (REQUIRED — not the official Lora-net fork) ----
# The official Lora-net/sx1302_hal is missing lgw_i2c_set_path / lgw_i2c_set_temp_sensor_addr
# Only brocaar's fork has these functions that concentratord v4.6+ requires.
HAL_DIR="${HOME}/sx1302_hal_brocaar"
if [[ ! -d "$HAL_DIR" ]]; then
  log "Cloning brocaar/sx1302_hal @ ${BROCAAR_HAL_TAG}..."
  git clone https://github.com/brocaar/sx1302_hal.git -b "$BROCAAR_HAL_TAG" "$HAL_DIR"
fi

log "Building sx1302_hal..."
cd "$HAL_DIR"
make libloragw

log "Installing HAL headers and libs to /usr/local..."
sudo mkdir -p /usr/local/include/libloragw-sx1302
sudo cp libloragw/inc/*.h /usr/local/include/libloragw-sx1302/
sudo cp libtools/inc/*.h /usr/local/include/
sudo cp libloragw/libloragw.a /usr/local/lib/libloragw-sx1302.a
sudo cp libtools/libtinymt32.a /usr/local/lib/libtinymt32.a
sudo ldconfig

# ---- chirpstack-concentratord ----
if [[ ! -d "$CONCENTRATORD_DIR" ]]; then
  log "Cloning chirpstack-concentratord..."
  git clone https://github.com/chirpstack/chirpstack-concentratord.git "$CONCENTRATORD_DIR"
fi

log "Building chirpstack-concentratord-sx1302 (this takes a few minutes)..."
cd "$CONCENTRATORD_DIR"
cargo clean
BINDGEN_EXTRA_CLANG_ARGS="-I/usr/local/include" \
RUSTFLAGS="-L/usr/local/lib" \
  cargo build --release -p chirpstack-concentratord-sx1302

log "Installing binary..."
sudo cp target/release/chirpstack-concentratord-sx1302 /usr/local/bin/
sudo chmod +x /usr/local/bin/chirpstack-concentratord-sx1302

log "Done! Binary at /usr/local/bin/chirpstack-concentratord-sx1302"
chirpstack-concentratord-sx1302 --version
