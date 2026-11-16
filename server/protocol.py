"""Framing: 4-byte little-endian uint32 length + UTF-8 JSON payload."""
import asyncio
import json
import struct

HEADER = struct.Struct("<I")
MAX_FRAME = 16 * 1024 * 1024


class ProtocolError(Exception):
    pass


class RawJson:
    """A JSON value already encoded (its text: real UTF-8, no \\u escapes), spliced into the frame as it is by encode():
    a tile's inline file (staging.inline_raw, read in the worker that built it) was parsed and encoded again on the
    event loop, ~10-20 ms of the interpreter per large tile file. Read as the value it holds (parsed on first use)."""
    __slots__ = ("text", "_value")
    _UNSET = object()

    def __init__(self, text):
        self.text = text
        self._value = RawJson._UNSET

    def value(self):
        if self._value is RawJson._UNSET:
            self._value = json.loads(self.text)
        return self._value

    def __getitem__(self, k):
        return self.value()[k]

    def __contains__(self, k):
        return k in self.value()

    def __iter__(self):
        return iter(self.value())

    def __len__(self):
        return len(self.value())

    def get(self, k, default=None):
        return self.value().get(k, default)

    def __eq__(self, other):
        return self.value() == (other.value() if isinstance(other, RawJson) else other)

    __hash__ = None


def encode(msg):
    # real UTF-8, not \u escapes: the game's jsonDecode leaves those as text ("Provence-Alpes-Cu00f4te d'Azur" in the status
    # bar, 2026-10-15)
    raw = [k for k, v in msg.items() if type(v) is RawJson]
    if raw:                 # (values already encoded: after the rest, as they are)
        rest = {k: v for k, v in msg.items() if type(v) is not RawJson}
        head = json.dumps(rest, separators=(",", ":"), ensure_ascii=False)
        body = ",".join(json.dumps(k, ensure_ascii=False) + ":" + msg[k].text for k in raw)
        payload = (head[:-1] + ("," if rest else "") + body + "}").encode("utf-8")
    else:
        payload = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return HEADER.pack(len(payload)) + payload


def decode_payload(payload):
    try:
        msg = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ProtocolError(f"bad JSON frame: {e}") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("frame is not an object with a 'type'")
    return msg


def _check_len(n):
    if n > MAX_FRAME:
        raise ProtocolError(f"frame too large: {n} bytes")
    return n


async def read_message(reader):
    """Next message from an asyncio StreamReader, or None on a clean EOF."""
    try:
        head = await reader.readexactly(HEADER.size)
    except asyncio.IncompleteReadError as e:
        if not e.partial:
            return None
        raise ProtocolError("connection closed inside a frame header") from None
    n = _check_len(HEADER.unpack(head)[0])
    try:
        payload = await reader.readexactly(n)
    except asyncio.IncompleteReadError:
        raise ProtocolError("connection closed inside a frame") from None
    return decode_payload(payload)


class Decoder:
    """Incremental decoder for blocking clients: feed() bytes, get messages."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= HEADER.size:
            n = _check_len(HEADER.unpack_from(self.buf)[0])
            if len(self.buf) < HEADER.size + n:
                break
            out.append(decode_payload(bytes(self.buf[HEADER.size:HEADER.size + n])))
            del self.buf[:HEADER.size + n]
        return out
