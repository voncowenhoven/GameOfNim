"""Pure Nim rules: board creation, move validation, and move application.

Nothing here touches sockets or shared state, so it is safe to call from
any thread and easy to unit test.
"""

import random

from protocol import PLAYER_1, PLAYER_2

DEFAULT_STACKS = (3, 5, 7)
MAX_TAKE = 3
MAX_RANDOM_STACK = 10


def new_stacks(randomize):
    """Return a fresh board, random sizes if requested."""
    if randomize:
        return [random.randint(1, MAX_RANDOM_STACK) for _ in DEFAULT_STACKS]
    return list(DEFAULT_STACKS)


def is_whole_number(value):
    """Return True for real ints, rejecting bools that JSON could sneak in."""
    return isinstance(value, int) and not isinstance(value, bool)


def move_error(stacks, stack, count):
    """Return why a move is illegal, or None if it is legal."""
    if not is_whole_number(stack) or not is_whole_number(count):
        return "Stack and count must be integers."
    if stack < 0 or stack >= len(stacks):
        return f"Stack must be between 1 and {len(stacks)}."
    if stacks[stack] == 0:
        return f"Stack {stack + 1} is empty."
    most = min(MAX_TAKE, stacks[stack])
    if count < 1 or count > most:
        return f"You may take 1 to {most} stones from stack {stack + 1}."
    return None


def apply_move(stacks, stack, count):
    """Return a new board with count stones removed from one stack."""
    new_board = list(stacks)
    new_board[stack] -= count
    return new_board


def is_board_empty(stacks):
    """Return True once the last stone has been taken."""
    return sum(stacks) == 0


def other_player(role):
    """Return the opponent of the given role."""
    if role == PLAYER_1:
        return PLAYER_2
    return PLAYER_1
