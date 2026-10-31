"""LSA encoding/flooding helpers and HMAC authentication of control frames.

LSA payload: origin(1) seq(4) n(1) { neighbor(1) cost(2) } * n  || mac(8)
Sequence numbers make flooding loop-free: a node re-floods only an LSA that is
newer than what its LSDB holds, and never back out the arrival link.
"""

import hashlib
import hmac
import struct

MAC_LEN = 8


def sign(secret: bytes, data: bytes) -> bytes:
    return hmac.new(secret, data, hashlib.sha256).digest()[:MAC_LEN]


def verify(secret: bytes, data: bytes, mac: bytes) -> bool:
    return hmac.compare_digest(sign(secret, data), mac)


def encode_lsa(secret, origin, seq, links):
    body = struct.pack(">BIB", origin, seq, len(links))
    for nbr, cost in sorted(links.items()):
        body += struct.pack(">BH", nbr, min(cost, 0xFFFF))
    return body + sign(secret, body)


def decode_lsa(secret, payload):
    """Return (origin, seq, links) or None if malformed / unauthenticated."""
    if len(payload) < 6 + MAC_LEN:
        return None
    body, mac = payload[:-MAC_LEN], payload[-MAC_LEN:]
    if not verify(secret, body, mac):
        return None
    origin, seq, n = struct.unpack_from(">BIB", body)
    if len(body) != 6 + 3 * n:
        return None
    links = {}
    for i in range(n):
        nbr, cost = struct.unpack_from(">BH", body, 6 + 3 * i)
        links[nbr] = cost
    return origin, seq, links
