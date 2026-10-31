"""Tiny L3/L4-style header carried inside DATA payloads (what the DPI parses).

    magic "AG"(2) ttl(1) proto(1) sport(2) dport(2) | application data
"""

import struct

MAGIC = b"AG"
HDR = struct.Struct(">2sBBHH")
PROTO_APP = 1
DEFAULT_TTL = 16
PORT_STREAM = 5000   # numbered test stream
PORT_CHAT = 5001     # free-form text


def pack(ttl, proto, sport, dport, data: bytes) -> bytes:
    return HDR.pack(MAGIC, ttl, proto, sport, dport) + data


def unpack(payload: bytes):
    """Return (ttl, proto, sport, dport, data) or None when malformed."""
    if len(payload) < HDR.size:
        return None
    magic, ttl, proto, sport, dport = HDR.unpack_from(payload)
    if magic != MAGIC:
        return None
    return ttl, proto, sport, dport, payload[HDR.size:]


def decrement_ttl(payload: bytes) -> bytes:
    return payload[:2] + bytes([payload[2] - 1]) + payload[3:]
