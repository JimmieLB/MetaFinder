"""Mini Duel: a tiny two-player deckbuilder with hidden hands and random draws.

Each turn: draw up to HAND_SIZE cards (CHANCE nodes), play attack/heal cards in
any order, buy at most one card with the coins in hand, then end the turn.
Reduce the opponent to 0 HP to win.

Exercises: hidden information (determinize), chance (chance_outcomes),
a custom rollout policy, telemetry events and a strategy_key.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

CHANCE = -1

# name: (cost, coins, damage, heal)
CARDS = {
    "Copper":   (0, 1, 0, 0),
    "Silver":   (3, 2, 0, 0),
    "Gold":     (6, 3, 0, 0),
    "Strike":   (2, 0, 1, 0),
    "Cleave":   (4, 0, 3, 0),
    "Fireball": (7, 0, 6, 0),
    "Bandage":  (4, 0, 0, 2),
}
MARKET = ["Silver", "Gold", "Strike", "Cleave", "Fireball", "Bandage"]


@dataclass(frozen=True)
class Draw:
    card: str

    def __str__(self):
        return f"draw {self.card}"


@dataclass(frozen=True)
class Play:
    card: str

    def __str__(self):
        return f"play {self.card}"


@dataclass(frozen=True)
class Buy:
    card: str

    def __str__(self):
        return f"buy {self.card}"


@dataclass(frozen=True)
class EndTurn:
    def __str__(self):
        return "end turn"


END = EndTurn()


def _count(cards: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in cards:
        out[c] = out.get(c, 0) + 1
    return out


class DuelState:
    __slots__ = ("cfg", "hp", "deck", "hand", "discard", "played", "to_move", "turn",
                 "bought", "to_draw", "events")

    def clone(self):
        s = DuelState.__new__(DuelState)
        s.cfg = self.cfg
        s.hp = self.hp[:]
        s.deck = [d[:] for d in self.deck]
        s.hand = [h[:] for h in self.hand]
        s.discard = [d[:] for d in self.discard]
        s.played = self.played[:]
        s.to_move, s.turn, s.bought, s.to_draw = self.to_move, self.turn, self.bought, self.to_draw
        s.events = []
        return s

    # ---------------------------------------------------------------- required
    def current_player(self):
        return CHANCE if self.to_draw > 0 else self.to_move

    def legal_actions(self):
        if self.to_draw > 0:
            return [Draw(c) for c in sorted(set(self.deck[self.to_move]))]
        hand = self.hand[self.to_move]
        actions = [Play(c) for c in sorted(set(hand)) if CARDS[c][2] or CARDS[c][3]]
        if not self.bought:
            coins = self.coins()
            actions += [Buy(c) for c in MARKET if CARDS[c][0] <= coins]
        actions.append(END)
        return actions

    def chance_outcomes(self):
        deck = self.deck[self.to_move]
        counts = _count(deck)
        return [(Draw(c), counts[c] / len(deck)) for c in sorted(counts)]

    def apply(self, a):
        p = self.to_move
        if isinstance(a, Draw):
            self.deck[p].remove(a.card)
            self.hand[p].append(a.card)
            self.to_draw -= 1
            self._refill_if_needed()
        elif isinstance(a, Play):
            self.hand[p].remove(a.card)
            self.played.append(a.card)
            _, _, dmg, heal = CARDS[a.card]
            self.hp[1 - p] -= dmg
            self.hp[p] = min(self.cfg["max_hp"], self.hp[p] + heal)
            self.events.append({"type": "card_played", "card": a.card, "player": p, "turn": self.turn})
        elif isinstance(a, Buy):
            self.bought = True
            self.discard[p].append(a.card)
            self.events.append({"type": "card_bought", "card": a.card, "player": p, "turn": self.turn})
        else:  # EndTurn
            self.discard[p] += self.hand[p] + self.played
            self.hand[p], self.played = [], []
            self.bought = False
            self.to_move = 1 - p
            self.turn += 1
            self.to_draw = self.cfg["hand_size"]
            self._refill_if_needed()

    def is_terminal(self):
        return min(self.hp) <= 0 or self.turn >= self.cfg["max_turns"]

    def returns(self):
        if self.hp[0] == self.hp[1]:
            return [0.5, 0.5]
        return [1.0, 0.0] if self.hp[0] > self.hp[1] else [0.0, 1.0]

    def determinize(self, observer, rng: random.Random):
        """The observer knows the opponent's hand+deck as a combined pool
        (all buys and discards are public) but not how it is split."""
        s = self.clone()
        opp = 1 - observer
        pool = s.hand[opp] + s.deck[opp]
        rng.shuffle(pool)
        k = len(s.hand[opp])
        s.hand[opp], s.deck[opp] = pool[:k], pool[k:]
        return s

    # ---------------------------------------------------------------- optional
    def coins(self):
        return sum(CARDS[c][1] for c in self.hand[self.to_move])

    def heuristic_value(self):
        diff = (self.hp[0] - self.hp[1]) / self.cfg["max_hp"]
        v = max(0.0, min(1.0, 0.5 + diff / 2))
        return [v, 1 - v]

    def drain_events(self):
        ev, self.events = self.events, []
        return ev

    def describe(self):
        p = self.to_move
        return (f"T{self.turn} P{p} HP {self.hp[0]}–{self.hp[1]} · hand: {', '.join(sorted(self.hand[p])) or '—'}"
                f" · {self.coins()} coins")

    def strategy_key(self):
        # Bucket decisions by the coins available, so the policy table answers
        # "what should I buy with N coins?"
        if self.bought:
            return "after buying"
        return f"{self.coins()} coins, buy phase"

    def to_json(self):
        return {"hp": self.hp, "deck": [sorted(d) for d in self.deck], "hand": [sorted(h) for h in self.hand],
                "discard": [sorted(d) for d in self.discard], "played": sorted(self.played),
                "to_move": self.to_move, "turn": self.turn, "bought": self.bought, "to_draw": self.to_draw}

    # ---------------------------------------------------------------- internals
    def _refill_if_needed(self):
        p = self.to_move
        if self.to_draw > 0 and not self.deck[p]:
            self.deck[p], self.discard[p] = self.discard[p], []
            if not self.deck[p]:
                self.to_draw = 0


class DuelGame:
    name = "Mini Duel (deckbuilder)"
    num_players = 2
    perfect_information = False
    max_steps = 1500
    rollout_depth = 60  # ~3 turns of lookahead, then heuristic_value()

    def new_game(self, seed, config):
        cfg = {"max_hp": 15, "hand_size": 4, "max_turns": 60, **config}
        for k in ("max_hp", "hand_size", "max_turns"):
            cfg[k] = int(cfg[k])
        for name, stats in (config.get("cards") or {}).items():
            CARDS[name] = tuple(stats)
        s = DuelState.__new__(DuelState)
        s.cfg = cfg
        s.hp = [cfg["max_hp"], cfg["max_hp"]]
        start = ["Copper"] * 6 + ["Strike"] * 3
        s.deck = [start[:], start[:]]
        s.hand = [[], []]
        s.discard = [[], []]
        s.played = []
        s.to_move, s.turn, s.bought = 0, 0, False
        s.to_draw = cfg["hand_size"]
        s.events = []
        return s

    def default_rollout_policy(self, state: DuelState, rng: random.Random):
        """Cheap greedy-ish policy: usually play damage, buy the priciest card, then end."""
        actions = state.legal_actions()
        if rng.random() < 0.2:
            return rng.choice(actions)
        plays = [a for a in actions if isinstance(a, Play)]
        if plays:
            return rng.choice(plays)
        buys = [a for a in actions if isinstance(a, Buy)]
        if buys:
            return max(buys, key=lambda b: (CARDS[b.card][0], rng.random()))
        return END


GAME = DuelGame()
