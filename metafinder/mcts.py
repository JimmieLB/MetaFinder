"""Single-observer Information-Set MCTS with chance nodes.

* Perfect-information games: identical to plain UCT.
* Hidden-information games: each iteration searches a fresh determinization
  of the root state, and UCB uses "availability" counts so actions that are
  only sometimes legal are not unfairly penalized (Cowling et al., 2012).
* Chance nodes: outcomes are sampled by probability; they are not selected.
* Perfect-information games also run an MCTS-Solver: terminal results are
  propagated as proven values, so forced wins/losses are found exactly.
"""
from __future__ import annotations

import math
import random
import time
from typing import Any, Callable, Hashable

CHANCE = -1


class Node:
    __slots__ = ("parent", "action", "player", "children", "visits", "value_sum", "avail",
                 "to_move", "n_legal", "proven")

    def __init__(self, parent: "Node | None", action: Hashable, player: int, n_players: int):
        self.parent = parent
        self.action = action
        self.player = player  # who took `action` to reach this node (CHANCE for outcomes)
        self.children: dict[Hashable, Node] = {}
        self.visits = 0
        self.value_sum = [0.0] * n_players
        self.avail = 1
        self.to_move: int | None = None  # who acts at this node's state (perfect info only)
        self.n_legal = 0
        self.proven: list[float] | None = None  # exact game value, once solved

    def mean(self, player: int) -> float:
        if self.proven is not None:
            return self.proven[player]
        return self.value_sum[player] / self.visits if self.visits else 0.0


def sample_chance(state, rng: random.Random) -> Hashable:
    outcomes = getattr(state, "chance_outcomes", None)
    if outcomes is not None:
        pairs = outcomes()
        r = rng.random() * sum(p for _, p in pairs)
        acc = 0.0
        for action, p in pairs:
            acc += p
            if r <= acc:
                return action
        return pairs[-1][0]
    return state.sample_chance(rng)


def terminal_or_heuristic(state, n_players: int) -> list[float]:
    if state.is_terminal():
        return list(state.returns())
    heuristic = getattr(state, "heuristic_value", None)
    if heuristic is not None:
        return list(heuristic())
    return [1.0 / n_players] * n_players


class MCTS:
    def __init__(
        self,
        n_players: int,
        perfect_information: bool,
        iterations: int = 1000,
        time_limit: float | None = None,
        exploration: float = 0.7,
        max_depth: int = 200,
        rollout_policy: Callable[[Any, random.Random], Hashable] | None = None,
        rng: random.Random | None = None,
    ):
        self.n_players = n_players
        self.perfect_information = perfect_information
        self.iterations = iterations
        self.time_limit = time_limit
        self.c = exploration
        self.max_depth = max_depth
        self.rollout_policy = rollout_policy
        self.rng = rng or random.Random()
        self.solver = perfect_information

    # ------------------------------------------------------------------ search
    def search(self, root_state, observer: int, on_progress: Callable[[int], None] | None = None) -> Node:
        root = Node(None, None, observer, self.n_players)
        deadline = time.perf_counter() + self.time_limit if self.time_limit else None
        report_every = max(1, self.iterations // 20)
        for i in range(self.iterations):
            if deadline and i % 32 == 0 and time.perf_counter() > deadline:
                break
            if on_progress and i % report_every == 0:
                on_progress(i)
            if self.perfect_information:
                state = root_state.clone()
            else:
                state = root_state.determinize(observer, self.rng)
            node, depth = self._select_expand(root, state)
            values = node.proven if node.proven is not None else self._rollout(state, depth)
            while node is not None:
                node.visits += 1
                for p in range(self.n_players):
                    node.value_sum[p] += values[p]
                if self.solver:
                    self._try_prove(node)
                node = node.parent
            if root.proven is not None:
                break  # game-theoretic value of the root is known; more search is pointless
        return root

    def _select_expand(self, node: Node, state) -> tuple[Node, int]:
        depth = 0
        rng = self.rng
        while depth < self.max_depth and not state.is_terminal():
            if node.proven is not None:
                return node, depth
            player = state.current_player()
            if player == CHANCE:
                outcome = sample_chance(state, rng)
                child = node.children.get(outcome)
                if child is None:
                    child = node.children[outcome] = Node(node, outcome, CHANCE, self.n_players)
                state.apply(outcome)
                node, depth = child, depth + 1
                continue

            legal = state.legal_actions()
            if self.solver:
                node.to_move, node.n_legal = player, len(legal)
                win = self._proven_win(node, player)
                if win is not None:
                    state.apply(win.action)
                    node, depth = win, depth + 1
                    continue
            untried = []
            for a in legal:
                child = node.children.get(a)
                if child is None:
                    untried.append(a)
                else:
                    child.avail += 1
            if untried:
                a = rng.choice(untried)
                child = node.children[a] = Node(node, a, player, self.n_players)
                state.apply(a)
                if self.solver and state.is_terminal():
                    child.proven = list(state.returns())
                return child, depth + 1

            best, best_score = None, -math.inf
            for a in legal:
                child = node.children[a]
                if child.proven is not None and child.proven[player] <= 1e-9 and best is not None:
                    continue  # proven loss: only pick it if nothing else exists
                score = child.mean(player) + self.c * math.sqrt(math.log(child.avail) / child.visits)
                if score > best_score:
                    best, best_score = child, score
            state.apply(best.action)
            node, depth = best, depth + 1
        if self.solver and state.is_terminal():
            node.proven = list(state.returns())
        return node, depth

    @staticmethod
    def _proven_win(node: Node, player: int) -> "Node | None":
        for child in node.children.values():
            if child.proven is not None and child.proven[player] >= 1 - 1e-9:
                return child
        return None

    def _try_prove(self, node: Node) -> None:
        p = node.to_move
        if node.proven is not None or p is None or p == CHANCE or not node.children:
            return
        win = self._proven_win(node, p)
        if win is not None:
            node.proven = win.proven
        elif len(node.children) == node.n_legal and all(c.proven is not None for c in node.children.values()):
            node.proven = max((c.proven for c in node.children.values()), key=lambda v: v[p])

    def _rollout(self, state, depth: int) -> list[float]:
        rng = self.rng
        policy = self.rollout_policy
        while depth < self.max_depth and not state.is_terminal():
            if state.current_player() == CHANCE:
                state.apply(sample_chance(state, rng))
            elif policy is not None:
                state.apply(policy(state, rng))
            else:
                state.apply(rng.choice(state.legal_actions()))
            depth += 1
        return terminal_or_heuristic(state, self.n_players)

    # ------------------------------------------------------------------ helpers
    def best_action(self, state, player: int) -> tuple[Hashable, Node]:
        root = self.search(state, player)
        legal = set(state.legal_actions())
        candidates = [c for a, c in root.children.items() if a in legal]
        if not candidates:  # should not happen, but never return an illegal move
            return self.rng.choice(list(legal)), root
        return rank_children(candidates, player)[0].action, root


def proof_status(node: Node, player: int) -> str | None:
    if node.proven is None:
        return None
    v = node.proven[player]
    return "win" if v >= 1 - 1e-9 else "loss" if v <= 1e-9 else "draw"


def rank_children(children, player: int) -> list[Node]:
    """Proven wins first, proven losses last, everything else by visit count."""
    order = {"win": 0, None: 1, "draw": 1, "loss": 2}
    return sorted(children, key=lambda c: (order[proof_status(c, player)], -c.visits, -c.mean(player)))
