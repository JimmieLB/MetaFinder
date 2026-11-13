"""Takeaway: players alternately remove 1..max_take stones; whoever takes the last stone wins.

Known optimal strategy: always leave a multiple of (max_take + 1) stones.
Useful for checking that MetaFinder recovers a known answer.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Take:
    n: int

    def __str__(self):
        return f"take {self.n}"


class TakeawayState:
    __slots__ = ("stones", "max_take", "to_move", "winner")

    def __init__(self, stones, max_take, to_move=0, winner=None):
        self.stones, self.max_take, self.to_move, self.winner = stones, max_take, to_move, winner

    # --- required
    def clone(self):
        return TakeawayState(self.stones, self.max_take, self.to_move, self.winner)

    def current_player(self):
        return self.to_move

    def legal_actions(self):
        return [Take(n) for n in range(1, min(self.max_take, self.stones) + 1)]

    def apply(self, action):
        self.stones -= action.n
        if self.stones == 0:
            self.winner = self.to_move
        self.to_move = 1 - self.to_move

    def is_terminal(self):
        return self.winner is not None

    def returns(self):
        return [1.0, 0.0] if self.winner == 0 else [0.0, 1.0]

    # --- optional
    def describe(self):
        return f"{self.stones} stones left, player {self.to_move} to move"

    def strategy_key(self):
        return f"{self.stones} stones left"

    def to_json(self):
        return {"stones": self.stones, "to_move": self.to_move, "winner": self.winner}


class TakeawayGame:
    name = "Takeaway"
    num_players = 2
    perfect_information = True
    max_steps = 200

    def new_game(self, seed, config):
        return TakeawayState(int(config.get("stones", 21)), int(config.get("max_take", 3)))


GAME = TakeawayGame()
