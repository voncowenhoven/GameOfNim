"""Wire protocol shared by client and server (see protocol_blueprint.md).

Every message is a compact UTF-8 JSON object terminated by a newline.
"""

import argparse
import json
from datetime import datetime, timezone

PLAYER_1 = "PLAYER_1"
PLAYER_2 = "PLAYER_2"
SERVER = "SERVER"
ROLES = (PLAYER_1, PLAYER_2)

MAX_MESSAGE_BYTES = 4096

CONNECT = "CONNECT"
LOBBY_WAIT = "LOBBY_WAIT"
GAME_START = "GAME_START"
MOVE = "MOVE"
STATE_UPDATE = "STATE_UPDATE"
ERROR = "ERROR"
DISCONNECT = "DISCONNECT"
GAME_OVER = "GAME_OVER"
REMATCH = "REMATCH"

MALFORMED_MESSAGE = "MALFORMED_MESSAGE"
OUT_OF_TURN = "OUT_OF_TURN"
INVALID_MOVE = "INVALID_MOVE"
WRONG_STATE = "WRONG_STATE"
LOBBY_FULL = "LOBBY_FULL"
MESSAGE_TOO_BIG = "MESSAGE_TOO_BIG"

LAST_OBJECT_TAKEN = "LAST_OBJECT_TAKEN"
FORFEIT = "FORFEIT"


class ProtocolError(ValueError):
    """A line could not be decoded into a valid message envelope."""


class MessageTooBig(ProtocolError):
    """A line exceeded MAX_MESSAGE_BYTES."""


class PeerClosed(ConnectionError):
    """The other side closed its end of the connection."""


def utc_timestamp():
    """Return the current UTC time, e.g. 2026-10-04T20:00:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_message(msg_type, player_id, payload):
    """Return a message envelope dict."""
    return {
        "msg_type": msg_type,
        "player_id": player_id,
        "payload": payload,
        "timestamp": utc_timestamp(),
    }


def encode_message(message):
    """Serialize a message to newline-terminated compact JSON bytes."""
    text = json.dumps(message, separators=(",", ":"))
    return text.encode("utf-8") + b"\n"


def decode_line(line):
    """Parse one line of bytes into a message dict or raise ProtocolError."""
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("Message is not valid UTF-8 JSON.") from error
    if not isinstance(message, dict):
        raise ProtocolError("Message must be a JSON object.")
    if not isinstance(message.get("msg_type"), str):
        raise ProtocolError("Message is missing a msg_type string.")
    if not isinstance(message.get("payload"), dict):
        raise ProtocolError("Message is missing a payload object.")
    return message


def split_lines(buffer):
    """Split buffered bytes into complete lines and the leftover partial."""
    *lines, remainder = buffer.split(b"\n")
    for piece in lines + [remainder]:
        if len(piece) > MAX_MESSAGE_BYTES:
            raise MessageTooBig("Message exceeds 4096 bytes.")
    return [line for line in lines if line.strip()], remainder


def send_message(sock, msg_type, player_id, payload):
    """Build, encode, and fully send one message."""
    message = build_message(msg_type, player_id, payload)
    sock.sendall(encode_message(message))


def port_number(text):
    """argparse type: an integer TCP port between 1 and 65535."""
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"'{text}' is not a number.") from None
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError("Port must be 1 to 65535.")
    return port


class LineReader:
    """Buffers bytes from one socket and hands back one line at a time."""

    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""
        self.pending = []

    def next_line(self):
        """Block until a complete line has arrived and return it."""
        while not self.pending:
            self.receive_once()
        return self.pending.pop(0)

    def receive_once(self):
        """Read whatever bytes are available and queue complete lines."""
        try:
            data = self.sock.recv(MAX_MESSAGE_BYTES)
        except TimeoutError:
            # Idle ticks are normal while a player thinks; not a dropped peer.
            return
        if not data:
            raise PeerClosed("Peer closed the connection.")
        lines, self.buffer = split_lines(self.buffer + data)
        self.pending.extend(lines)
