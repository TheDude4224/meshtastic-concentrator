"""
ChirpStack Concentratord gw.Command protobuf encoder.
Field numbers verified against generated Rust prost structs.

gw.Command { send_downlink_frame = tag 1 }
DownlinkFrame { downlink_id=3, items=5(repeated), gateway_id=7 }
DownlinkFrameItem { phy_payload=1, tx_info=3 }
DownlinkTxInfo { frequency=1, power=2, modulation=3, board=4, antenna=5, timing=6 }
Modulation { lora=3 (NOT 5!) }
LoraModulationInfo { bandwidth=1, spreading_factor=2, code_rate_legacy=3,
                     polarization_inversion=4, code_rate(enum)=5, preamble=6 }
Timing { immediately=1 }
ImmediatelyTimingInfo {} (empty)
CodeRate enum: CR_4_5=1, CR_4_6=2, CR_4_7=3, CR_4_8=4
"""


def _varint(v: int) -> bytes:
    if v < 0:
        v &= 0xFFFFFFFFFFFFFFFF
    out = []
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def _fv(num, val):   return _varint((num << 3) | 0) + _varint(val)
def _fb(num, data):  return _varint((num << 3) | 2) + _varint(len(data)) + data
def _fs(num, s):     return _fb(num, s.encode())


CODE_RATES = {"4/5": 1, "4/6": 2, "4/7": 3, "4/8": 4}


def build_command(phy_payload: bytes,
                  frequency: int,
                  power: int,
                  bandwidth: int,
                  spreading_factor: int,
                  code_rate: str = "4/8",
                  preamble: int = 16,
                  downlink_id: int = 1,
                  gateway_id: str = "") -> bytes:
    """
    Build gw.Command { send_downlink_frame: DownlinkFrame { ... } }
    Returns single bytes for one ZMQ frame.
    """
    cr = CODE_RATES.get(code_rate, 4)

    # LoraModulationInfo
    lora_info = (
        _fv(1, bandwidth) +           # bandwidth
        _fv(2, spreading_factor) +    # spreading_factor
        _fs(3, code_rate) +           # code_rate_legacy (string e.g. "4/8")
        _fv(4, 0) +                   # polarization_inversion = false
        _fv(5, cr) +                  # code_rate enum
        _fv(6, preamble)              # preamble
    )

    # Modulation { lora = field 3 }
    modulation = _fb(3, lora_info)

    # ImmediatelyTimingInfo is empty; Timing { immediately = field 1 }
    timing = _fb(1, b'')

    # DownlinkTxInfo
    tx_info = (
        _fv(1, frequency) +           # frequency
        _fv(2, power) +               # power
        _fb(3, modulation) +          # modulation
        _fb(6, timing)                # timing
    )

    # DownlinkFrameItem
    item = _fb(1, phy_payload) + _fb(3, tx_info)

    # DownlinkFrame
    dl_frame = _fv(3, downlink_id) + _fb(5, item)
    if gateway_id:
        dl_frame += _fs(7, gateway_id)

    # gw.Command { send_downlink_frame = field 1 }
    return _fb(1, dl_frame)
