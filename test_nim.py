"""Unit tests for the pure game rules, the framing code, and client text."""

import unittest

from client import parse_move_input, render_board, turn_status
from nim_game import apply_move, is_board_empty, move_error, new_stacks
from protocol import (
    MOVE, PLAYER_1, PLAYER_2, MessageTooBig, ProtocolError, build_message,
    decode_line, encode_message, split_lines,
)


class MoveErrorTests(unittest.TestCase):

    def test_legal_move_has_no_error(self):
        self.assertIsNone(move_error([3, 5, 7], 2, 3))

    def test_empty_stack_is_rejected(self):
        self.assertIsNotNone(move_error([0, 5, 7], 0, 1))

    def test_count_outside_one_to_three_is_rejected(self):
        self.assertIsNotNone(move_error([3, 5, 7], 2, 0))
        self.assertIsNotNone(move_error([3, 5, 7], 2, 4))

    def test_count_above_stack_size_is_rejected(self):
        self.assertIsNotNone(move_error([2, 5, 7], 0, 3))

    def test_bad_index_is_rejected(self):
        self.assertIsNotNone(move_error([3, 5, 7], 3, 1))
        self.assertIsNotNone(move_error([3, 5, 7], -1, 1))

    def test_non_integers_are_rejected(self):
        self.assertIsNotNone(move_error([3, 5, 7], True, 1))
        self.assertIsNotNone(move_error([3, 5, 7], "1", 1))
        self.assertIsNotNone(move_error([3, 5, 7], 0, None))


class BoardTests(unittest.TestCase):

    def test_apply_move_does_not_mutate_input(self):
        board = [3, 5, 7]
        self.assertEqual(apply_move(board, 2, 3), [3, 5, 4])
        self.assertEqual(board, [3, 5, 7])

    def test_board_empty_only_when_all_zero(self):
        self.assertTrue(is_board_empty([0, 0, 0]))
        self.assertFalse(is_board_empty([0, 1, 0]))

    def test_default_and_random_boards(self):
        self.assertEqual(new_stacks(False), [3, 5, 7])
        for size in new_stacks(True):
            self.assertTrue(1 <= size <= 10)


class FramingTests(unittest.TestCase):

    def test_round_trip(self):
        message = build_message(MOVE, PLAYER_1, {"stack": 2, "count": 3})
        encoded = encode_message(message)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertNotIn(b" ", encoded)
        self.assertEqual(decode_line(encoded.rstrip(b"\n")), message)

    def test_garbage_is_rejected(self):
        for line in (b"not json", b"[1, 2]", b'{"payload": {}}',
                     b'{"msg_type": "MOVE", "payload": 5}', b"\xff"):
            with self.assertRaises(ProtocolError):
                decode_line(line)

    def test_split_keeps_partial_remainder(self):
        lines, rest = split_lines(b'{"a":1}\n{"b":2}\n{"c"')
        self.assertEqual(lines, [b'{"a":1}', b'{"b":2}'])
        self.assertEqual(rest, b'{"c"')

    def test_oversized_line_is_rejected(self):
        with self.assertRaises(MessageTooBig):
            split_lines(b"x" * 4097)


class ClientTextTests(unittest.TestCase):

    def test_parse_move_input(self):
        self.assertEqual(parse_move_input(" 3 2 "), (2, 2))
        self.assertIsNone(parse_move_input("3"))
        self.assertIsNone(parse_move_input("a b"))
        self.assertIsNone(parse_move_input("-1 2"))

    def test_turn_status(self):
        self.assertIn("Your Turn", turn_status(PLAYER_1, PLAYER_1))
        self.assertIn("Waiting for PLAYER_2", turn_status(PLAYER_1, PLAYER_2))

    def test_render_board_draws_columns(self):
        lines = render_board([3, 0, 7]).splitlines()
        self.assertEqual(len(lines), 7 + 3)
        self.assertEqual(lines[0].count("[#]"), 1)
        self.assertEqual(lines[6].count("[#]"), 2)
        self.assertIn("(0)", lines[-1])

    def test_render_empty_board_shows_only_footer(self):
        lines = render_board([0, 0, 0]).splitlines()
        self.assertEqual(len(lines), 3)


if __name__ == "__main__":
    unittest.main()
