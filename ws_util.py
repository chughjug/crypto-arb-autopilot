"""Minimal WebSocket frame helpers (RFC 6455, server-side, unmasked outbound)."""

from __future__ import annotations

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_guid() -> str:
    return _WS_GUID


def ws_send_frame(wfile, opcode: int, data: bytes) -> None:
    b0 = 0x80 | (opcode & 0x0F)
    n = len(data)
    if n < 126:
        header = bytes([b0, n])
    elif n < 65536:
        header = bytes([b0, 126]) + n.to_bytes(2, "big")
    else:
        header = bytes([b0, 127]) + n.to_bytes(8, "big")
    wfile.write(header + data)
    wfile.flush()


def ws_recv_frame(rfile):
    head = rfile.read(2)
    if len(head) < 2:
        return None
    b0, b1 = head[0], head[1]
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    ln = b1 & 0x7F
    if ln == 126:
        ln = int.from_bytes(rfile.read(2), "big")
    elif ln == 127:
        ln = int.from_bytes(rfile.read(8), "big")
    mask = rfile.read(4) if masked else b"\x00\x00\x00\x00"
    payload = bytearray(rfile.read(ln))
    if masked:
        for i in range(ln):
            payload[i] ^= mask[i % 4]
    return opcode, bytes(payload)
