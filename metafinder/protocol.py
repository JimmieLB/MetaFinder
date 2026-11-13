"""The contract a game module must satisfy to be analyzed by MetaFinder.

A game repo exposes a `GameModule` object through `metafinder.json` at its root:

    { "entry": "my_package.my_module:GAME" }

Only the methods marked REQUIRED are needed. Everything else is optional and
detected at runtime with getattr().
"""
from __future__ import annotations

import random
from typing import Any, Hashable, Protocol, Sequence

PlayerId = int
CHANCE: PlayerId = -1  # "nature" is to move: shuffles, draws, dice rolls


class GameState(Protocol):
    # ---- REQUIRED -------------------------------------------------------
    def clone(self) -> "GameState": ...  # deep, independent copy; must be cheap
    def current_player(self) -> PlayerId: ...  # 0..n-1, or CHANCE
    def legal_actions(self) -> Sequence[Hashable]: ...  # stable, hashable values; deterministic order
    def apply(self, action: Hashable) -> None: ...  # mutate in place; deterministic
    def is_terminal(self) -> bool: ...
    def returns(self) -> Sequence[float]: ...  # one value per player in [0, 1]; 1 = win

    # ---- REQUIRED if the game has CHANCE nodes ---------------------------
    def chance_outcomes(self) -> Sequence[tuple[Hashable, float]]: ...  # (outcome, probability)
    # alternatively: def sample_chance(self, rng: random.Random) -> Hashable

    # ---- REQUIRED if perfect_information is False -----------------------
    def determinize(self, observer: PlayerId, rng: random.Random) -> "GameState": ...
    # Return a clone where everything `observer` cannot see is re-randomized
    # consistently with what they *can* see.

    # ---- OPTIONAL -------------------------------------------------------
    def heuristic_value(self) -> Sequence[float]: ...  # used when a rollout hits the depth cap
    def drain_events(self) -> list[dict[str, Any]]: ...  # telemetry, e.g. {"type": "card_bought", ...}
    def to_json(self) -> Any: ...  # used by the conformance checks and replays
    def describe(self) -> str: ...  # one-line human-readable state
    def action_label(self, action: Hashable) -> str: ...  # defaults to str(action)


class GameModule(Protocol):
    name: str  # REQUIRED
    num_players: int  # REQUIRED
    perfect_information: bool  # REQUIRED
    max_steps: int  # optional, default 500: hard cap on actions per game
    rollout_depth: int  # optional: cut rollouts after this many steps and use heuristic_value()

    def new_game(self, seed: int, config: dict) -> GameState: ...  # REQUIRED
    def default_rollout_policy(self, state: GameState, rng: random.Random) -> Hashable: ...  # optional
