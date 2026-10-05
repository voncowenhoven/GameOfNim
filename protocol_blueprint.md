# Nim Application Protocol Blueprint

# CS 457, Sprint 1 Game: Nim (2-player, turn-based, terminal client) Transport: TCP Serialization: UTF-8 JSON Framing: Newline-delimited JSON, one object per line, as God intended

# Rules
The board is a list of three stacks that represents a non-negative number of stones, default state [3, 5, 7], though each of the three stacks can have a random amount up to 10 if the game is started with the --random flag passed.

Players alternate turns. On a turn, the active player removes at least 1 stone from exactly one non-empty stack. They may remove up to three stones from any one stack. The player who removes the last object wins. There are no draws in Nim, whoever picks up the last stone wins.

PLAYER_1 moves first in round 1. The first mover alternates on each rematch.
The server is authoritative. Clients only ever render STATE_UPDATE messages.

# Transport/Framing
Every message is a serialized, compacted JSON object encoded as UTF-8 and terminated with \n.

# Message Types
"PLAYER_1" is the first player to connect, and "PLAYER_2" is the next player after that.

# Message Envelopes/Style
Every message between server and client follows this schema;

| Key | Type | Description |
|---|---|---|
| msg_type | string | One of the message types below |
| player_id | string or null | Sender. "PLAYER_1" or "PLAYER_2" for client messages, "SERVER" for server messages. This is null on CONNECT until the role is assigned. |
| payload | object | Message-specific fields in JSON |
| timestamp | string | UTC time the message was sent, e.g. "2026-10-04T20:00:00Z" |

CONNECT (Client -> Server): Join the game. Must be the first message sent or else the game couldn't happen. Duh.
Payload: none ({})

Example: `{"msg_type":"CONNECT","player_id":null,"payload":{},"timestamp":"2026-10-04T20:00:00Z"}`

LOBBY_WAIT (Server -> Client): Tells the first player the server is waiting for an opponent.
Payload: message (string)

Example: `{"msg_type":"LOBBY_WAIT","player_id":"SERVER","payload":{"message":"Waiting for an opponent..."},"timestamp":"2026-10-04T20:00:01Z"}`

GAME_START (Server -> Both): Starts a round, tells each client its role, and states which player moves first.
Payload: your_role (string), opponent (string), stacks (int array), first_turn (string)

Example: `{"msg_type":"GAME_START","player_id":"SERVER","payload":{"your_role":"PLAYER_1","opponent":"PLAYER_2","stacks":[3,5,7],"first_turn":"PLAYER_1"},"timestamp":"2026-10-04T20:00:05Z"}`

MOVE (Client -> Server): Removes count stones from the chosen stack. This should only be considered if the player requesting it is the active player.
Payload: stack (int, zero-indexed, 0 <= stack <= 2, stack must be non-empty), count (int, 1 <= count <= min(3, stones in that stack))

Example: `{"msg_type":"MOVE","player_id":"PLAYER_1","payload":{"stack":2,"count":3},"timestamp":"2026-10-04T20:00:12Z"}`

STATE_UPDATE (Server -> Both): Sent after GAME_START and after every legal move.
Payload: stacks (int array), active_player (string, or null if the game ended/player left or crashed), last_move (object {by: string, stack: int, count: int}, or null), scores (object {PLAYER_1: int, PLAYER_2: int}, rounds won)

Example: `{"msg_type":"STATE_UPDATE","player_id":"SERVER","payload":{"stacks":[3,5,4],"active_player":"PLAYER_2","last_move":{"by":"PLAYER_1","stack":2,"count":3},"scores":{"PLAYER_1":0,"PLAYER_2":0}},"timestamp":"2026-10-04T20:00:13Z"}`

ERROR (Server -> Client): Rejects a bad message. Sent to the client that caused the error. Fatal errors will end the game.
Payload: code (string), message (string), fatal (boolean)
Non-fatal codes: MALFORMED_MESSAGE (message couldn't be decoded), OUT_OF_TURN (move was requested when it wasn't the player's turn), INVALID_MOVE (player tried to request an invalid move), WRONG_STATE (move couldn't be processed in this state)
Fatal codes: LOBBY_FULL (the lobby is full), MESSAGE_TOO_BIG (the message exceeds 4096 bytes)

Example: `{"msg_type":"ERROR","player_id":"SERVER","payload":{"code":"OUT_OF_TURN","message":"It is PLAYER_1's turn.","fatal":false},"timestamp":"2026-10-04T20:00:20Z"}`

DISCONNECT (Client -> Server): The player is quitting on purpose. The client closes its socket right after sending.
Payload: none ({})

Example: `{"msg_type":"DISCONNECT","player_id":"PLAYER_1","payload":{},"timestamp":"2026-10-04T20:02:30Z"}`

GAME_OVER (Server -> Both): Announces the winner of the round. On a forfeit, it goes only to the remaining player.
Payload: winner (string role), reason (string: "LAST_OBJECT_TAKEN" or "FORFEIT"), scores (object), rematch_available (boolean)

Example: `{"msg_type":"GAME_OVER","player_id":"SERVER","payload":{"winner":"PLAYER_1","reason":"LAST_OBJECT_TAKEN","scores":{"PLAYER_1":1,"PLAYER_2":0},"rematch_available":true},"timestamp":"2026-10-04T20:01:55Z"}`

REMATCH (Client -> Server): Vote to play another round after GAME_OVER. If both accept, the server resets all stacks (or randomizes them if --random was passed at the start of the program) and sends GAME_START again with scores carried over. If either declines, both connections close.
Payload: accept (boolean)

Example: `{"msg_type":"REMATCH","player_id":"PLAYER_2","payload":{"accept":true},"timestamp":"2026-10-04T20:02:00Z"}`

# Terminated Connections
Graceful path: When a client disconnects, it sends DISCONNECT, then closes its TCP socket with TCP FIN. The server marks the remaining player as the winner by forfeit, sends them GAME_OVER with reason "FORFEIT" and rematch_available false, and cleans up.

Bad disconnects that don't send DISCONNECT: recv() returns b"" (EOF, meaning the peer closed its side of the connection). The TCP receiving loop checks `if not data:` and goes down the graceful path. This keeps the game from hanging the server and consuming 100% CPU power.

Abrupt drop: The next recv()/send()/whatever raises one of these, which the receive loop catches to trigger forfeit and cleanup:
- ConnectionResetError: the peer crashed or forcibly closed
- BrokenPipeError: the server tried to send() to a socket whose other end is already closed
- ConnectionAbortedError: the connection was aborted by the local OS
- TimeoutError: the peer stopped responding past the socket timeout

Disconnects by state: If a client disconnects in the lobby, nothing happens and the lobby goes back to waiting for a player. Mid-game will crown the other player the winner, and the results screen will just go back to the lobby.

Fatal errors: After sending a fatal: true ERROR the server closes that socket, and the other player is told that the disconnector forfeited the match.