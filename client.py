"""Nim game client: connects to the server and plays from the terminal.

"""

import argparse
import socket
import sys

from protocol import (
    CONNECT, DISCONNECT, ERROR, FORFEIT, GAME_OVER, GAME_START,
    LAST_OBJECT_TAKEN, LOBBY_WAIT, MOVE, REMATCH, STATE_UPDATE,
    LineReader, PeerClosed, ProtocolError, decode_line, port_number,
    send_message,
)

CONNECT_TIMEOUT_SECONDS = 10
RECV_TICK_SECONDS = 1.0
DIVIDER = "=" * 44
STONE = "[#]"
STACK_BASE = "-------"
COLUMN_WIDTH = 10
REASON_TEXT = {
    LAST_OBJECT_TAKEN: "took the last stone, making them the King of Stones!",
    FORFEIT: "won by forfeit, prevailing against the other cowardly player!",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Nim game client.")
    parser.add_argument("-i", dest="hostname", required=True,
                        help="server DNS hostname, e.g. server.conover.edu")
    parser.add_argument("-p", "--port", type=port_number, required=True,
                        help="server port")
    return parser.parse_args()


def resolve_server(hostname):
    """Resolve the server hostname via DNS or exit with status 1."""
    try:
        return socket.gethostbyname(hostname)
    except (socket.gaierror, UnicodeError):
        print(f"[ERROR] Unable to resolve DNS hostname '{hostname}'.")
        print("Please check your Cisco router DNS configuration "
              "('ip dns server' / 'ip host') or verify domain spelling.")
        sys.exit(1)


def connect_to_server(server_ip, port):
    """Open a TCP connection to the server or exit with status 1."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT_SECONDS)
    try:
        sock.connect((server_ip, port))
    except OSError as error:
        print(f"[ERROR] Could not connect to {server_ip}:{port} ({error}).")
        print("Check that the server is running and reachable.")
        sock.close()
        sys.exit(1)
    # Short recv timeouts keep Ctrl+C responsive on Windows.
    sock.settimeout(RECV_TICK_SECONDS)
    return sock


def parse_move_input(text):
    """Turn '3 2' into zero-indexed (2, 2), or None if unreadable."""
    parts = text.split()
    if len(parts) != 2:
        return None
    if not parts[0].isdecimal() or not parts[1].isdecimal():
        return None
    return int(parts[0]) - 1, int(parts[1])


def stack_cell(size, height):
    """Return what one stack shows at a given height above its base."""
    if size >= height:
        return STONE.center(COLUMN_WIDTH)
    return " " * COLUMN_WIDTH


def board_row(cells):
    """Join one row of column cells into an indented line."""
    return "  " + "".join(cells).rstrip()


def board_footer(stacks):
    """Return the base, stack labels, and stone counts under the columns."""
    bases = [STACK_BASE.center(COLUMN_WIDTH) for _ in stacks]
    labels = [f"Stack {number}".center(COLUMN_WIDTH)
              for number in range(1, len(stacks) + 1)]
    counts = [f"({size})".center(COLUMN_WIDTH) for size in stacks]
    return [board_row(bases), board_row(labels), board_row(counts)]


def render_board(stacks):
    """Return the stacks drawn as vertical columns of ASCII stones."""
    rows = []
    for height in range(max(stacks, default=0), 0, -1):
        cells = [stack_cell(size, height) for size in stacks]
        rows.append(board_row(cells))
    return "\n".join(rows + board_footer(stacks))


def role_label(role, my_role):
    """Return the role name, marking the local player."""
    if role == my_role:
        return f"{role} (you)"
    return role


def format_scores(scores, my_role):
    """Return a one-line score summary."""
    parts = [f"{role_label(role, my_role)}: {wins}"
             for role, wins in scores.items()]
    return "  Score  " + "  |  ".join(parts)


def turn_status(my_role, active_player):
    """Return whose turn it is from the local player's view."""
    if active_player is None:
        return "  Round over."
    if active_player == my_role:
        return "  >>> Your Turn <<<"
    return f"  Waiting for {active_player}..."


def describe_last_move(last_move, my_role):
    """Return a sentence about the previous move, or None."""
    if last_move is None:
        return None
    who = role_label(last_move["by"], my_role)
    return (f"  {who} took {last_move['count']} "
            f"from stack {last_move['stack'] + 1}.")


def winner_headline(winner, my_role):
    """Return the round result from the local player's view."""
    if winner == my_role:
        return "You win!"
    return f"{winner} wins."


def print_state(payload, my_role):
    """Print the board, scores, and turn status."""
    print()
    last = describe_last_move(payload.get("last_move"), my_role)
    if last is not None:
        print(last)
    print(render_board(payload["stacks"]))
    print(format_scores(payload["scores"], my_role))
    print(turn_status(my_role, payload["active_player"]))


def print_round_start(payload):
    """Print the banner that opens a round."""
    print(f"\n{DIVIDER}")
    print(f"  NEW ROUND: you are {payload['your_role']} "
          f"vs {payload['opponent']}")
    print(f"  {payload['first_turn']} moves first. Take 1-3 stones "
          "from one stack.")
    print("  Whoever takes the last stone wins.")
    print(DIVIDER)


def print_game_over(payload, my_role):
    """Print the winner, why they won, and the scores."""
    reason = REASON_TEXT.get(payload["reason"], payload["reason"])
    print(f"\n{DIVIDER}")
    print(f"  GAME OVER: {winner_headline(payload['winner'], my_role)} "
          f"({payload['winner']} {reason})")
    print(format_scores(payload["scores"], my_role))
    print(DIVIDER)


def ask_yes_no(question):
    """Prompt until the player answers y or n."""
    while True:
        answer = input(question).strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


class NimClient:
    """One player's connection and the latest state the server sent."""

    def __init__(self, sock):
        self.sock = sock
        self.reader = LineReader(sock)
        self.role = None
        self.active_player = None
        self.handlers = {
            LOBBY_WAIT: self.on_lobby_wait,
            GAME_START: self.on_game_start,
            STATE_UPDATE: self.on_state_update,
            ERROR: self.on_error,
            GAME_OVER: self.on_game_over,
        }

    def run(self):
        """Join the game and react to server messages forever."""
        self.send(CONNECT, {})
        while True:
            self.handle_line(self.reader.next_line())

    def send(self, msg_type, payload):
        """Send one message tagged with our role."""
        send_message(self.sock, msg_type, self.role, payload)

    def handle_line(self, line):
        """Decode one server line and dispatch it."""
        try:
            message = decode_line(line)
        except ProtocolError:
            print("[WARN] Ignoring an unreadable message from the server.")
            return
        handler = self.handlers.get(message["msg_type"])
        if handler is not None:
            handler(message["payload"])

    def on_lobby_wait(self, payload):
        """Tell the player we are waiting for an opponent."""
        print(f"\n[LOBBY] {payload.get('message', 'Waiting...')}")

    def on_game_start(self, payload):
        """Learn our role and announce the new round."""
        self.role = payload["your_role"]
        print_round_start(payload)

    def on_state_update(self, payload):
        """Show the board and take our turn if it is ours."""
        self.active_player = payload["active_player"]
        print_state(payload, self.role)
        if self.active_player == self.role:
            self.take_turn()

    def on_error(self, payload):
        """Show a server error; exit if fatal, retry the turn if not."""
        print(f"[ERROR] {payload.get('code')}: {payload.get('message')}")
        if payload.get("fatal"):
            self.leave("The server ended the session.", 1)
        if self.active_player == self.role:
            self.take_turn()

    def on_game_over(self, payload):
        """Show the result and vote on a rematch when offered."""
        self.active_player = None
        print_game_over(payload, self.role)
        if payload.get("rematch_available"):
            self.vote_rematch()

    def vote_rematch(self):
        """Send our rematch vote; declining ends the match."""
        accept = ask_yes_no("Play again? (y/n): ")
        self.send(REMATCH, {"accept": accept})
        if not accept:
            self.leave("Thanks for playing!")
        print("Waiting for your opponent's answer...")

    def take_turn(self):
        """Ask the player for a move and send it to the server."""
        stack, count = self.prompt_move()
        self.send(MOVE, {"stack": stack, "count": count})

    def prompt_move(self):
        """Prompt until the player types a readable move or quits."""
        while True:
            text = input("Your move (<stack> <count>, q to quit): ").strip()
            if text.lower() == "q":
                self.quit()
            move = parse_move_input(text)
            if move is not None:
                return move
            print("Type two numbers, e.g. '3 2' takes 2 from stack 3.")

    def quit(self):
        """Tell the server we are leaving on purpose, then exit."""
        try:
            self.send(DISCONNECT, {})
        except OSError:
            pass
        self.leave("You left the game.")

    def leave(self, message, status=0):
        """Close the socket, print a farewell, and exit."""
        self.sock.close()
        print(message)
        sys.exit(status)


def main():
    """Resolve the server, connect, and play until the session ends."""
    args = parse_args()
    server_ip = resolve_server(args.hostname)
    print(f"Resolved {args.hostname} to {server_ip}")
    client = NimClient(connect_to_server(server_ip, args.port))
    try:
        client.run()
    except (KeyboardInterrupt, EOFError):
        client.quit()
    except PeerClosed:
        client.leave("The server closed the connection.")
    except OSError as error:
        client.leave(f"Connection lost: {type(error).__name__}", 1)


if __name__ == "__main__":
    main()
