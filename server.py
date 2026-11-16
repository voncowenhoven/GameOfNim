"""Nim game server: accepts two players and runs the game FSM.

The main thread accepts connections and each player gets a worker thread.
All shared game state is guarded by one lock (see concurrency_design.md).
"""

import argparse
import socket
import sys
import threading
from enum import Enum

from nim_game import apply_move, is_board_empty, move_error, new_stacks
from nim_game import other_player
from protocol import (
    CONNECT, DISCONNECT, ERROR, FORFEIT, GAME_OVER, GAME_START,
    INVALID_MOVE, LAST_OBJECT_TAKEN, LOBBY_FULL, LOBBY_WAIT,
    MALFORMED_MESSAGE, MESSAGE_TOO_BIG, MOVE, OUT_OF_TURN, PLAYER_1,
    PLAYER_2, REMATCH, ROLES, SERVER, STATE_UPDATE, WRONG_STATE,
    LineReader, MessageTooBig, ProtocolError, decode_line, port_number,
    send_message,
)

LISTEN_HOST = "0.0.0.0"
LISTEN_BACKLOG = 5
SOCKET_TIMEOUT_SECONDS = 10
ACCEPT_TICK_SECONDS = 1.0
LOBBY_MESSAGE = "Waiting for an opponent..."


class State(Enum):
    """Server FSM states from fsm_specification.md."""

    INIT = "INIT"
    WAITING_FOR_PLAYERS = "WAITING_FOR_PLAYERS"
    GAME_START = "GAME_START"
    PLAYER_TURN = "PLAYER_TURN"
    GAME_OVER = "GAME_OVER"
    CLEANUP = "CLEANUP"


class ClientLeft(Exception):
    """The client's session is over and its worker should tear down."""


def parse_args():
    """Parse -p and --random from the command line."""
    parser = argparse.ArgumentParser(description="Nim game server.")
    parser.add_argument("-p", "--port", type=port_number, required=True,
                        help="port to listen on (binds 0.0.0.0)")
    parser.add_argument("--random", action="store_true",
                        help="start each round with random stacks up to 10")
    return parser.parse_args()


def log(text):
    """Print a server event immediately so piped logs stay in order."""
    print(f"[SERVER] {text}", flush=True)


def error_payload(code, message, fatal):
    """Return the payload of an ERROR message."""
    return {"code": code, "message": message, "fatal": fatal}


def shutdown_quietly(sock):
    """Shut a socket down, ignoring sockets that are already dead."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def close_socket(sock):
    """Send FIN and release the socket."""
    shutdown_quietly(sock)
    sock.close()


def open_listener(port):
    """Create the listening TCP socket bound to every interface."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_REUSEADDR causes a crash on Windows so we avoid it here.
    if sys.platform != "win32":
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((LISTEN_HOST, port))
    listener.listen(LISTEN_BACKLOG)
    # A timeout lets Ctrl+C interrupt accept() on Windows.
    listener.settimeout(ACCEPT_TICK_SECONDS)
    log(f"Listening on {LISTEN_HOST}:{port}")
    return listener


class NimServer:
    """Shared game state plus the threads that act on it.

    Methods ending in _locked must only be called while holding world_lock.
    """

    def __init__(self, randomize):
        self.randomize = randomize
        self.world_lock = threading.Lock()
        self.state = State.INIT
        self.sockets = {PLAYER_1: None, PLAYER_2: None}
        self.connected = set()
        self.stacks = new_stacks(randomize)
        self.active_player = None
        self.last_move = None
        self.first_turn = PLAYER_1
        self.scores = {PLAYER_1: 0, PLAYER_2: 0}
        self.rematch_votes = set()
        self.handlers = {
            MOVE: self.handle_move,
            REMATCH: self.handle_rematch,
            DISCONNECT: self.handle_disconnect,
            CONNECT: self.handle_connect,
        }

    def set_state_locked(self, new_state):
        """Move the FSM to a new state and log the transition."""
        print(f"[FSM] {self.state.value} -> {new_state.value}", flush=True)
        self.state = new_state

    def serve(self, port):
        """Listen on the port and accept clients until interrupted."""
        listener = open_listener(port)
        with self.world_lock:
            self.set_state_locked(State.WAITING_FOR_PLAYERS)
        try:
            self.accept_loop(listener)
        finally:
            listener.close()

    def accept_loop(self, listener):
        """Hand every new connection to admit_client."""
        while True:
            try:
                conn, address = listener.accept()
            except TimeoutError:
                continue
            log(f"Connection from {address[0]}:{address[1]}")
            self.admit_client(conn)

    def admit_client(self, conn):
        """Give the connection a free slot and a worker, or reject it."""
        conn.settimeout(SOCKET_TIMEOUT_SECONDS)
        with self.world_lock:
            role = self.reserve_slot_locked(conn)
        if role is None:
            reject_full_lobby(conn)
            return
        log(f"Reserved slot {role}")
        worker = threading.Thread(target=self.run_worker,
                                  args=(conn, role), daemon=True)
        worker.start()

    def reserve_slot_locked(self, conn):
        """Return the first free role after claiming it, or None."""
        for role in ROLES:
            if self.sockets[role] is None:
                self.sockets[role] = conn
                return role
        return None

    def run_worker(self, conn, role):
        """Serve one client from handshake to teardown."""
        reader = LineReader(conn)
        try:
            self.await_connect(reader, role)
            self.receive_loop(reader, role)
        except MessageTooBig:
            log(f"{role} sent an oversized message")
            self.reply_error(role, MESSAGE_TOO_BIG,
                             "Message exceeds 4096 bytes.", True)
        except ClientLeft as reason:
            log(f"{role} left: {reason}")
        except OSError as error:
            log(f"{role} dropped: {type(error).__name__}")
        finally:
            self.teardown(conn, role)

    def await_connect(self, reader, role):
        """Require CONNECT as the first message, then register the player."""
        try:
            message = decode_line(reader.next_line())
        except ProtocolError:
            message = {"msg_type": None}
        if message["msg_type"] != CONNECT:
            self.reply_error(role, WRONG_STATE,
                             "The first message must be CONNECT.", True)
            raise ClientLeft("handshake failed")
        self.register_player(role)

    def register_player(self, role):
        """Mark the player connected and start a round once both are in."""
        with self.world_lock:
            self.connected.add(role)
            log(f"{role} joined")
            if len(self.connected) < len(ROLES):
                self.send_to_locked(role, LOBBY_WAIT,
                                    {"message": LOBBY_MESSAGE})
                return
            self.start_round_locked()

    def receive_loop(self, reader, role):
        """Handle messages from this client until its session ends."""
        while True:
            self.handle_line(reader.next_line(), role)

    def handle_line(self, line, role):
        """Decode one line and dispatch it to its message handler."""
        try:
            message = decode_line(line)
        except ProtocolError as error:
            self.reply_error(role, MALFORMED_MESSAGE, str(error), False)
            return
        handler = self.handlers.get(message["msg_type"])
        if handler is None:
            self.reply_error(role, MALFORMED_MESSAGE,
                             "Unknown msg_type.", False)
            return
        handler(role, message["payload"])

    def handle_move(self, role, payload):
        """Validate and apply a MOVE in one critical section."""
        with self.world_lock:
            problem = self.validate_move_locked(role, payload)
            if problem is not None:
                code, text = problem
                self.send_to_locked(role, ERROR,
                                    error_payload(code, text, False))
                return
            self.apply_move_locked(role, payload["stack"], payload["count"])

    def validate_move_locked(self, role, payload):
        """Return (code, message) describing a bad move, or None."""
        if self.state != State.PLAYER_TURN:
            return WRONG_STATE, "No round is in progress."
        if role != self.active_player:
            return OUT_OF_TURN, f"It is {self.active_player}'s turn."
        reason = move_error(self.stacks, payload.get("stack"),
                            payload.get("count"))
        if reason is not None:
            return INVALID_MOVE, reason
        return None

    def apply_move_locked(self, role, stack, count):
        """Update the board, then end the round or pass the turn."""
        self.stacks = apply_move(self.stacks, stack, count)
        self.last_move = {"by": role, "stack": stack, "count": count}
        log(f"{role} took {count} from stack {stack + 1}: {self.stacks}")
        if is_board_empty(self.stacks):
            self.finish_round_locked(role)
            return
        self.active_player = other_player(role)
        self.broadcast_locked(STATE_UPDATE, self.state_payload_locked())

    def finish_round_locked(self, winner):
        """Score the round and announce the winner to both players."""
        self.scores[winner] += 1
        self.active_player = None
        self.set_state_locked(State.GAME_OVER)
        self.broadcast_locked(STATE_UPDATE, self.state_payload_locked())
        payload = self.game_over_payload_locked(winner, LAST_OBJECT_TAKEN,
                                                True)
        self.broadcast_locked(GAME_OVER, payload)

    def handle_rematch(self, role, payload):
        """Record a rematch vote, or end the match on a decline."""
        with self.world_lock:
            if self.state != State.GAME_OVER:
                self.send_to_locked(role, ERROR, error_payload(
                    WRONG_STATE, "No rematch vote is open.", False))
                return
            if payload.get("accept") is not True:
                self.end_match_locked(role)
                raise ClientLeft("declined the rematch")
            self.record_vote_locked(role)

    def record_vote_locked(self, role):
        """Start a new round once both players have accepted."""
        self.rematch_votes.add(role)
        log(f"{role} accepted the rematch")
        if len(self.rematch_votes) < len(ROLES):
            return
        self.first_turn = other_player(self.first_turn)
        self.start_round_locked()

    def end_match_locked(self, decliner):
        """Reset the match and drop the opponent after a declined rematch."""
        opponent = other_player(decliner)
        opponent_socket = self.sockets[opponent]
        self.cleanup_match_locked()
        self.sockets[opponent] = None
        self.connected.discard(opponent)
        if opponent_socket is not None:
            shutdown_quietly(opponent_socket)

    def handle_connect(self, role, payload):
        """Reject a second CONNECT from an already registered player."""
        self.reply_error(role, WRONG_STATE, "Already connected.", False)

    def handle_disconnect(self, role, payload):
        """End the session of a player who chose to leave."""
        raise ClientLeft("sent DISCONNECT")

    def start_round_locked(self):
        """Reset the board and tell both players a round has begun."""
        self.set_state_locked(State.GAME_START)
        self.stacks = new_stacks(self.randomize)
        self.active_player = self.first_turn
        self.last_move = None
        self.rematch_votes.clear()
        for role in ROLES:
            self.send_to_locked(role, GAME_START,
                                self.game_start_payload_locked(role))
        self.set_state_locked(State.PLAYER_TURN)
        self.broadcast_locked(STATE_UPDATE, self.state_payload_locked())

    def teardown(self, conn, role):
        """Free the player's slot, settle the match, close the socket."""
        with self.world_lock:
            # A declined rematch may have already handed this slot onward.
            if self.sockets[role] is conn:
                self.sockets[role] = None
                self.connected.discard(role)
                self.release_survivor_locked(role)
        close_socket(conn)
        log(f"{role} disconnected")

    def release_survivor_locked(self, leaver):
        """Award a forfeit if mid-round and send the survivor to the lobby."""
        survivor = other_player(leaver)
        if self.state == State.PLAYER_TURN:
            payload = self.game_over_payload_locked(survivor, FORFEIT, False)
            self.send_to_locked(survivor, GAME_OVER, payload)
        if self.state in (State.PLAYER_TURN, State.GAME_OVER):
            self.cleanup_match_locked()
            self.send_to_locked(survivor, LOBBY_WAIT,
                                {"message": LOBBY_MESSAGE})

    def cleanup_match_locked(self):
        """Forget the finished match so the next pair starts fresh."""
        self.set_state_locked(State.CLEANUP)
        self.scores = {PLAYER_1: 0, PLAYER_2: 0}
        self.first_turn = PLAYER_1
        self.active_player = None
        self.last_move = None
        self.rematch_votes.clear()
        self.set_state_locked(State.WAITING_FOR_PLAYERS)

    def reply_error(self, role, code, message, fatal):
        """Send an ERROR to one player from outside a critical section."""
        with self.world_lock:
            self.send_to_locked(role, ERROR,
                                error_payload(code, message, fatal))

    def broadcast_locked(self, msg_type, payload):
        """Send the same message to both players in role order."""
        for role in ROLES:
            self.send_to_locked(role, msg_type, payload)

    def send_to_locked(self, role, msg_type, payload):
        """Send to one player, dropping them if the send fails."""
        sock = self.sockets[role]
        if sock is None:
            return
        try:
            send_message(sock, msg_type, SERVER, payload)
        except OSError as error:
            # Their own worker notices the shutdown and runs the forfeit.
            log(f"Send to {role} failed: {type(error).__name__}")
            shutdown_quietly(sock)

    def state_payload_locked(self):
        """Return the payload of a STATE_UPDATE message."""
        return {
            "stacks": list(self.stacks),
            "active_player": self.active_player,
            "last_move": self.last_move,
            "scores": dict(self.scores),
        }

    def game_start_payload_locked(self, role):
        """Return the GAME_START payload addressed to one role."""
        return {
            "your_role": role,
            "opponent": other_player(role),
            "stacks": list(self.stacks),
            "first_turn": self.first_turn,
        }

    def game_over_payload_locked(self, winner, reason, rematch_available):
        """Return the payload of a GAME_OVER message."""
        return {
            "winner": winner,
            "reason": reason,
            "scores": dict(self.scores),
            "rematch_available": rematch_available,
        }


def reject_full_lobby(conn):
    """Tell a third client the lobby is full and hang up."""
    log("Lobby full, rejecting connection")
    try:
        send_message(conn, ERROR, SERVER, error_payload(
            LOBBY_FULL, "The lobby is full.", True))
    except OSError:
        pass
    close_socket(conn)


def main():
    """Parse arguments and run the server until Ctrl+C."""
    args = parse_args()
    server = NimServer(args.random)
    try:
        server.serve(args.port)
    except KeyboardInterrupt:
        log("Shutting down")
    except OSError as error:
        print(f"[ERROR] Could not listen on port {args.port}: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
