"""Wire framing: 0x7E delimiters, 0x7D byte stuffing, CRC-32 trailer.

Layout (before stuffing), big-endian:
    FLAG | src(1) dst(1) type(1) seq(2) ack(2) len(2) | payload(0..1024) | crc32(4) | FLAG
The CRC covers header + payload. Everything between the two FLAG bytes is
byte-stuffed: 0x7E -> 0x7D 0x5E, 0x7D -> 0x7D 0x5D.
One UDP datagram carries exactly one frame.
"""

import struct
from dataclasses import dataclass

from .crc32 import crc32

FLAG = 0x7E
ESC = 0x7D
ESC_XOR = 0x20

T_DATA = 0x01
T_ACK = 0x02
T_NACK = 0x03
T_LSA = 0x04
T_HEARTBEAT = 0x05
T_QUARANTINE = 0x06

TYPE_NAMES = {T_DATA: "DATA", T_ACK: "ACK", T_NACK: "NACK", T_LSA: "LSA_FLOOD",
              T_HEARTBEAT: "HEARTBEAT", T_QUARANTINE: "QUARANTINE"}

MAX_PAYLOAD = 1024
HEADER = struct.Struct(">BBBHHH")
BROADCAST = 0xFF


class FrameError(Exception):
    """Frame could not be parsed."""


class BadDelimiter(FrameError):
    pass


class BadCRC(FrameError):
    pass


class BadLength(FrameError):
    pass


@dataclass
class Frame:
    src: int
    dst: int
    ftype: int
    seq: int = 0
    ack: int = 0
    payload: bytes = b""

    @property
    def type_name(self):
        return TYPE_NAMES.get(self.ftype, "0x%02X" % self.ftype)


def stuff(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == FLAG or b == ESC:
            out.append(ESC)
            out.append(b ^ ESC_XOR)
        else:
            out.append(b)
    return bytes(out)


def unstuff(data: bytes) -> bytes:
    out = bytearray()
    it = iter(data)
    for b in it:
        if b == ESC:
            nxt = next(it, None)
            if nxt is None:
                raise FrameError("dangling escape byte")
            out.append(nxt ^ ESC_XOR)
        elif b == FLAG:
            raise FrameError("unescaped flag inside frame")
        else:
            out.append(b)
    return bytes(out)


def encode(frame: Frame) -> bytes:
    if len(frame.payload) > MAX_PAYLOAD:
        raise BadLength("payload %d > %d" % (len(frame.payload), MAX_PAYLOAD))
    body = HEADER.pack(frame.src, frame.dst, frame.ftype, frame.seq & 0xFFFF,
                       frame.ack & 0xFFFF, len(frame.payload)) + frame.payload
    body += struct.pack(">I", crc32(body))
    return bytes([FLAG]) + stuff(body) + bytes([FLAG])


def decode(wire: bytes) -> Frame:
    if len(wire) < 2 or wire[0] != FLAG or wire[-1] != FLAG:
        raise BadDelimiter("missing 0x7E delimiters")
    body = unstuff(wire[1:-1])
    if len(body) < HEADER.size + 4:
        raise BadLength("frame too short")
    src, dst, ftype, seq, ack, plen = HEADER.unpack_from(body)
    if plen > MAX_PAYLOAD or len(body) != HEADER.size + plen + 4:
        raise BadLength("payload length mismatch")
    (got,) = struct.unpack(">I", body[-4:])
    if crc32(body[:-4]) != got:
        raise BadCRC("crc mismatch")
    return Frame(src, dst, ftype, seq, ack, body[HEADER.size:HEADER.size + plen])


def annotate(wire: bytes) -> dict:
    """Describe a raw wire frame for the hex inspector (never raises)."""
    info = {"hex": wire.hex(), "len": len(wire), "valid": True, "error": None}
    try:
        f = decode(wire)
        info.update(src=f.src, dst=f.dst, type=f.type_name, seq=f.seq, ack=f.ack,
                    plen=len(f.payload), stuffed=len(wire) - 2 - (HEADER.size + len(f.payload) + 4))
        info["crc"] = "%08X" % crc32(wire_body(wire))
    except FrameError as e:
        info["valid"] = False
        info["error"] = "%s: %s" % (type(e).__name__, e)
    return info


def wire_body(wire: bytes) -> bytes:
    return unstuff(wire[1:-1])[:-4]
