"""Analysis worker. Runs in its own process because it executes the game repo's code.

Usage: python -m metafinder.worker --repo DIR --params params.json

Protocol: one JSON object per line on stdout:
  {"type": "progress", "phase": str, "pct": float, "message": str}
  {"type": "result", ...}
  {"type": "error", "message": str, "traceback": str}
Anything the game prints goes to stderr so it cannot corrupt the protocol.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import random
import sys
import time
import traceback
from collections import Counter, defaultdict

from .mcts import CHANCE, MCTS, proof_status, rank_children, sample_chance

_PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr


def emit(obj) -> None:
    _PROTOCOL_OUT.write(json.dumps(obj, default=str) + "\n")
    _PROTOCOL_OUT.flush()


def progress(phase: str, pct: float, message: str = "") -> None:
    emit({"type": "progress", "phase": phase, "pct": round(pct, 3), "message": message})


class GameError(Exception):
    pass


# --------------------------------------------------------------------------- loading
def load_game(repo_dir: str):
    manifest_path = os.path.join(repo_dir, "metafinder.json")
    manifest: dict = {}
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        entry = manifest.get("entry")
        if not entry:
            raise GameError('metafinder.json must contain an "entry" like "my_module:GAME"')
    elif os.path.isfile(os.path.join(repo_dir, "metafinder_game.py")):
        entry = "metafinder_game:GAME"
    else:
        raise GameError(
            "No metafinder.json (or metafinder_game.py) found at the repo root. "
            "See the module format on the home page."
        )

    module_name, _, attr = entry.partition(":")
    sys.path.insert(0, os.path.abspath(repo_dir))
    try:
        module = importlib.import_module(module_name)
    except Exception as e:
        raise GameError(f"Could not import '{module_name}': {e!r}") from e
    game = getattr(module, attr or "GAME", None)
    if game is None:
        raise GameError(f"'{module_name}' has no attribute '{attr or 'GAME'}'")
    if callable(game) and not hasattr(game, "new_game"):
        game = game()

    missing = [a for a in ("name", "num_players", "perfect_information", "new_game") if not hasattr(game, a)]
    if missing:
        raise GameError(f"Game module is missing required attributes: {', '.join(missing)}")
    return game, manifest.get("config", {})


# --------------------------------------------------------------------------- helpers
def label(state, action) -> str:
    fn = getattr(state, "action_label", None)
    return str(fn(action)) if fn else str(action)


def describe(state) -> str | None:
    fn = getattr(state, "describe", None)
    return str(fn()) if fn else None


def fingerprint(state):
    to_json = getattr(state, "to_json", None)
    if to_json is not None:
        return json.dumps(to_json(), sort_keys=True, default=str)
    if state.is_terminal():
        return ("terminal", tuple(state.returns()))
    p = state.current_player()
    if p == CHANCE:
        return ("chance",)
    return (p, tuple(repr(a) for a in state.legal_actions()))


def drain(state) -> list[dict]:
    fn = getattr(state, "drain_events", None)
    return fn() if fn else []


def event_key(ev: dict) -> str:
    parts = [str(ev.get("type", "event"))]
    for k, v in sorted(ev.items()):
        if k in ("type", "player", "turn", "tick", "t", "step") or isinstance(v, (dict, list)):
            continue
        parts.append(f"{k}={v}")
    return " · ".join(parts)


class Clock:
    """Time budget for one analysis, so a job finishes with partial results instead of being killed.
    `soft` = don't start another game after this; `hard` = cut the game in progress."""

    def __init__(self, budget: float | None):
        self.start = time.perf_counter()
        self.budget = budget if budget and budget > 0 else None
        self.self_play_per_game = 0.0

    def at(self, fraction: float) -> float | None:
        return None if self.budget is None else self.start + self.budget * fraction

    @staticmethod
    def can_start(soft: float | None, per_game: float) -> bool:
        return soft is None or time.perf_counter() + per_game <= soft


# --------------------------------------------------------------------------- phase 1: conformance
def validate(game, config: dict, max_steps: int, n_games: int = 6) -> list[dict]:
    issues: list[dict] = []
    rng = random.Random(1234)
    n = game.num_players

    def add(severity, msg):
        if not any(i["message"] == msg for i in issues):
            issues.append({"severity": severity, "message": msg})

    for g in range(n_games):
        progress("validate", g / n_games, f"Random playthrough {g + 1}/{n_games}")
        try:
            s = game.new_game(g, dict(config))
        except Exception as e:
            raise GameError(f"new_game() crashed: {e!r}") from e
        history, steps = [], 0
        try:
            while not s.is_terminal() and steps < max_steps:
                p = s.current_player()
                if p == CHANCE:
                    if hasattr(s, "chance_outcomes"):
                        pairs = s.chance_outcomes()
                        if not pairs:
                            add("error", "chance_outcomes() returned no outcomes")
                            break
                        total = sum(prob for _, prob in pairs)
                        if abs(total - 1.0) > 1e-6:
                            add("warning", f"Chance probabilities sum to {total:.4f}, not 1")
                    elif not hasattr(s, "sample_chance"):
                        raise GameError("State reached a CHANCE node but has neither chance_outcomes() nor sample_chance()")
                    a = sample_chance(s, rng)
                else:
                    if not (0 <= p < n):
                        add("error", f"current_player() returned {p}, expected 0..{n - 1} or -1 (CHANCE)")
                        break
                    legal = list(s.legal_actions())
                    if not legal:
                        add("error", "A non-terminal state has no legal actions")
                        break
                    if len(set(legal)) != len(legal):
                        add("warning", "legal_actions() contains duplicates")
                    if steps % 4 == 0:
                        before = fingerprint(s)
                        c = s.clone()
                        c.apply(legal[-1])
                        if fingerprint(s) != before:
                            add("error", "clone() is not independent: applying an action to the clone changed the original")
                        if not game.perfect_information:
                            if not hasattr(s, "determinize"):
                                raise GameError("perfect_information is False but the state has no determinize()")
                            d = s.determinize(p, rng)
                            if d.current_player() != p or set(d.legal_actions()) != set(legal):
                                add("warning", "determinize() changed the observer's own legal actions (it may be leaking or losing information)")
                            if fingerprint(s) != before:
                                add("error", "determinize() mutated the original state; it must return a new state")
                    a = rng.choice(legal)
                s.apply(a)
                history.append(a)
                drain(s)
                steps += 1
        except GameError:
            raise
        except Exception as e:
            raise GameError(f"Game crashed during a random playthrough at step {steps}: {e!r}\n{traceback.format_exc()}") from e

        if steps >= max_steps:
            add("warning", f"A random playthrough did not finish within {max_steps} steps (treated as a timeout)")
        else:
            r = list(s.returns())
            if len(r) != n:
                add("error", f"returns() has {len(r)} values, expected {n}")
            elif any(v < -1e-9 or v > 1 + 1e-9 for v in r):
                add("warning", f"returns() should be in [0, 1], got {r}")

        replay = game.new_game(g, dict(config))
        for a in history:
            replay.apply(a)
        if fingerprint(replay) != fingerprint(s):
            add("error", "Replaying the same seed and actions gave a different state (hidden randomness? use CHANCE nodes or the seed)")

    if not issues:
        issues.append({"severity": "ok", "message": f"{n_games} random playthroughs passed all checks"})
    return issues


# --------------------------------------------------------------------------- phase 2: opening analysis
def analyze_opening(game, config, params, max_steps, clock):
    rng = random.Random(params["seed"])
    s = game.new_game(params["seed"], dict(config))
    steps = 0
    while s.current_player() == CHANCE and not s.is_terminal() and steps < max_steps:
        s.apply(sample_chance(s, rng))
        steps += 1
    drain(s)
    if s.is_terminal():
        return None
    player = s.current_player()

    total = params["opening_iterations"]
    engine = make_engine(game, params, rng, iterations=total)
    if clock.budget is not None:
        engine.time_limit = clock.budget * 0.1  # leave the rest for self-play
    msg = f"Deep search from the opening position ({total:,} iterations)"
    t0 = time.perf_counter()
    root = engine.search(s, player, on_progress=lambda i: progress("opening", i / total, msg))
    elapsed = time.perf_counter() - t0

    legal = set(s.legal_actions())
    children = rank_children([c for a, c in root.children.items() if a in legal], player)
    moves = [
        {
            "action": label(s, c.action),
            "visits": c.visits,
            "share": c.visits / max(1, root.visits),
            "value": c.mean(player),
            "proven": proof_status(c, player),
        }
        for c in children
    ]

    # Principal variation: follow the most-visited child.
    pv = []
    node = root
    walk = s.clone() if game.perfect_information else s.determinize(player, rng)
    while node.children and len(pv) < 14:
        if node.to_move is not None and node.to_move != CHANCE:
            child = rank_children(node.children.values(), node.to_move)[0]
        else:
            child = max(node.children.values(), key=lambda c: c.visits)
        if child.visits < 8 and child.proven is None:
            break
        is_chance = child.player == CHANCE
        if not is_chance and child.action not in set(walk.legal_actions()):
            break
        pv.append({
            "player": "chance" if is_chance else child.player,
            "action": label(walk, child.action),
            "value": None if is_chance else child.mean(child.player),
            "visits": child.visits,
        })
        walk.apply(child.action)
        node = child
        if walk.is_terminal():
            break

    return {
        "player": player,
        "position": describe(s),
        "iterations": root.visits,
        "seconds": elapsed,
        "iterations_per_second": root.visits / max(1e-6, elapsed),
        "moves": moves,
        "principal_variation": pv,
        "proven": proof_status(root, player),
        "cut": root.visits < total and root.proven is None,
    }


def make_engine(game, params, rng, iterations=None):
    rollout = getattr(game, "default_rollout_policy", None)
    return MCTS(
        n_players=game.num_players,
        perfect_information=game.perfect_information,
        iterations=iterations or params["iterations"],
        exploration=params["exploration"],
        max_depth=params["rollout_depth"],
        rollout_policy=rollout if params.get("use_module_rollout", True) else None,
        rng=rng,
        reuse_tree=params.get("tree_reuse", True),
    )


# --------------------------------------------------------------------------- phase 3/4: games
def play_game(game, config, seed, seat_agents, max_steps, record=False, stats=None, stop_at=None):
    """seat_agents[p] is an MCTS engine or None (uniform random). If the clock passes `stop_at`
    (time.perf_counter() seconds) the game is cut short and scored like a step-cap timeout."""
    rng = random.Random(seed * 7919 + 1)
    s = game.new_game(seed, dict(config))
    steps = 0
    transcript = []
    events = Counter()
    strategy_key = getattr(s, "strategy_key", None)
    cut = False
    engines = list({id(e): e for e in seat_agents if e is not None}.values())
    for e in engines:
        e.reset_tree()
    while not s.is_terminal() and steps < max_steps:
        if stop_at is not None and time.perf_counter() > stop_at:
            cut = True
            break
        p = s.current_player()
        if p == CHANCE:
            a = sample_chance(s, rng)
            if record and len(transcript) < 200:
                transcript.append({"player": "chance", "action": label(s, a)})
        else:
            agent = seat_agents[p]
            legal = s.legal_actions()
            if agent is None:
                a = rng.choice(legal)
            else:
                pos = describe(s) if record else None
                key = s.strategy_key() if (stats is not None and strategy_key is not None) else None
                a, root = agent.best_action(s, p)
                if stats is not None:
                    lab = label(s, a)
                    for x in legal:
                        stats["actions"][label(s, x)]["available"] += 1
                    stats["actions"][lab]["chosen"] += 1
                    stats["actions"][lab]["by_seat"][p] += 1
                    if key is not None:
                        stats["policy"][str(key)][lab] += 1
                        stats["policy_proof"][str(key)][proof_status(root, p) or "unsolved"] += 1
                if record and len(transcript) < 200:
                    ranked = sorted(root.children.values(), key=lambda c: -c.visits)[:3]
                    transcript.append({
                        "player": p,
                        "position": pos,
                        "action": label(s, a),
                        "value": root.children[a].mean(p) if a in root.children else None,
                        "alternatives": [
                            {"action": label(s, c.action), "share": c.visits / max(1, root.visits), "value": c.mean(p)}
                            for c in ranked if c.action != a and c.action in set(legal)
                        ][:2],
                    })
        s.apply(a)
        for e in engines:
            e.advance(a)  # keeps each engine's tree in step with the game
        for ev in drain(s):
            events[event_key(ev)] += 1
        steps += 1
    timed_out = not s.is_terminal()
    if timed_out:
        h = getattr(s, "heuristic_value", None)
        returns = list(h()) if h else [1.0 / game.num_players] * game.num_players
    else:
        returns = list(s.returns())
    return {"returns": returns, "steps": steps, "timed_out": timed_out and not cut, "cut": cut, "events": events,
            "transcript": transcript, "final": describe(s)}


def self_play(game, config, params, max_steps, clock):
    requested = params["games"]
    soft, hard = clock.at(0.7), clock.at(0.85)
    n = game.num_players
    stats = {
        "actions": defaultdict(lambda: {"available": 0, "chosen": 0, "by_seat": [0] * n}),
        "policy": defaultdict(Counter),
        "policy_proof": defaultdict(Counter),
    }
    seat_returns = [[] for _ in range(n)]
    lengths, timeouts = [], 0
    events = Counter()
    sample = None
    cut = 0
    t0 = time.perf_counter()
    for g in range(requested):
        per = (time.perf_counter() - t0) / g if g else 0.0
        if g and not clock.can_start(soft, per):
            break  # out of time: report the games played so far
        eta = f" · ~{per * (requested - g):.0f}s left" if g else ""
        progress("selfplay", g / requested, f"MCTS vs MCTS, game {g + 1}/{requested}{eta}")
        agents = [make_engine(game, params, random.Random(params["seed"] + 1000 * g + p)) for p in range(n)]
        res = play_game(game, config, params["seed"] + g, agents, max_steps, record=(g == 0), stats=stats, stop_at=hard)
        for p in range(n):
            seat_returns[p].append(res["returns"][p])
        lengths.append(res["steps"])
        timeouts += res["timed_out"]
        cut += res["cut"]
        events.update(res["events"])
        if g == 0:
            sample = {"moves": res["transcript"], "returns": res["returns"], "final": res["final"],
                      "timed_out": res["timed_out"] or res["cut"]}
    n_games = len(lengths)
    clock.self_play_per_game = (time.perf_counter() - t0) / n_games

    actions = []
    for name, a in stats["actions"].items():
        actions.append({
            "action": name,
            "available": a["available"],
            "chosen": a["chosen"],
            "pick_rate": a["chosen"] / a["available"] if a["available"] else 0.0,
            "by_seat": a["by_seat"],
        })
    actions.sort(key=lambda x: (-x["chosen"], -x["available"]))

    policy = []
    for key, counter in stats["policy"].items():
        total = sum(counter.values())
        best, cnt = counter.most_common(1)[0]
        proof = stats["policy_proof"][key]
        solved = proof.most_common(1)[0][0] if proof and proof.most_common(1)[0][1] == total else None
        policy.append({"situation": key, "best_action": best, "confidence": cnt / total, "samples": total,
                       "solved": None if solved == "unsolved" else solved,
                       "others": [{"action": a, "count": c} for a, c in counter.most_common(4)[1:]]})
    policy.sort(key=lambda r: _natural_key(r["situation"]))

    return {
        "games": n_games,
        "games_requested": requested,
        "cut_games": cut,
        "seat_avg_return": [sum(r) / len(r) for r in seat_returns],
        "seat_ci95": [_ci95(r) for r in seat_returns],
        "avg_length": sum(lengths) / len(lengths),
        "min_length": min(lengths),
        "max_length": max(lengths),
        "timeouts": timeouts,
        "actions": actions,
        "policy": policy,
        "events": [{"event": k, "count": v, "per_game": v / n_games} for k, v in events.most_common(40)],
        "sample_game": sample,
        "seconds": time.perf_counter() - t0,
    }


def vs_random(game, config, params, max_steps, clock):
    n = game.num_players
    requested = max(2, params["games"] // 2)
    soft, hard = clock.at(0.95), clock.at(0.97)
    mcts_scores, random_scores = [], []
    t0 = time.perf_counter()
    for g in range(requested):
        # An MCTS-vs-random game costs about 1/n of a self-play game, so use that as the first estimate.
        per = (time.perf_counter() - t0) / g if g else clock.self_play_per_game / n
        if not clock.can_start(soft, per):
            break
        progress("baseline", g / requested, f"MCTS vs random, game {g + 1}/{requested}")
        seat = g % n
        agents = [None] * n
        agents[seat] = make_engine(game, params, random.Random(params["seed"] + 50_000 + g))
        res = play_game(game, config, params["seed"] + 10_000 + g, agents, max_steps, stop_at=hard)
        if res["cut"]:
            break  # a half-played game says little about skill
        mcts_scores.append(res["returns"][seat])
        if n == 1:
            rnd = play_game(game, config, params["seed"] + 10_000 + g, [None], max_steps)
            random_scores.append(rnd["returns"][0])
        else:
            others = [res["returns"][p] for p in range(n) if p != seat]
            random_scores.append(sum(others) / len(others))
    if not mcts_scores:
        return None
    return {
        "games": len(mcts_scores),
        "games_requested": requested,
        "mcts_avg_return": sum(mcts_scores) / len(mcts_scores),
        "random_avg_return": sum(random_scores) / len(random_scores),
    }


def _ci95(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return 1.96 * math.sqrt(var / len(xs))


def _natural_key(s: str):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


# --------------------------------------------------------------------------- insights
def build_insights(game, opening, sp, base, params) -> list[dict]:
    out = []
    short = []
    if opening and opening.get("cut"):
        short.append(f"the opening search stopped at {opening['iterations']:,} of {params['opening_iterations']:,} iterations")
    if sp and sp["games"] < sp["games_requested"]:
        short.append(f"{sp['games']} of {sp['games_requested']} self-play games were played")
    if sp and sp["cut_games"]:
        short.append(f"{sp['cut_games']} self-play game(s) were cut short and scored by heuristic")
    if not base:
        short.append("the skill check against random players was skipped")
    elif base["games"] < base["games_requested"]:
        short.append(f"{base['games']} of {base['games_requested']} skill-check games were played")
    if short:
        out.append({"kind": "design", "text": f"**Time budget reached** ({params.get('time_budget') or 0:.0f}s): "
                    f"{'; '.join(short)}. Results are less reliable. Lower iterations per move or max_turns, "
                    "or raise METAFINDER_JOB_TIMEOUT."})
    if opening and opening["moves"]:
        best = opening["moves"][0]
        line = f"Best opening for player {opening['player']}: **{best['action']}**, estimated win value {best['value']:.0%}"
        losing = [m for m in opening["moves"][1:] if m["proven"] == "loss"]
        if best["proven"] == "win":
            line = f"Solved: **{best['action']}** is a forced win for player {opening['player']} from the opening."
        elif losing and len(losing) == len(opening["moves"]) - 1:
            line = (f"Best opening for player {opening['player']}: **{best['action']}**. Every alternative "
                    f"({', '.join(m['action'] for m in losing)}) is a proven forced loss.")
        elif opening["proven"] == "loss":
            line = f"Solved: player {opening['player']} loses against perfect play from the opening, whatever they do."
        elif len(opening["moves"]) > 1:
            second = opening["moves"][1]
            gap = best["value"] - second["value"]
            line += f" (next best: {second['action']} at {second['value']:.0%}"
            line += ", a clear favourite)" if gap > 0.1 else ", close call)"
        out.append({"kind": "strategy", "text": line})
        if best["value"] > 0.9:
            out.append({"kind": "balance", "text": f"The first player appears to have a forced or near-forced win from the opening (value {best['value']:.0%})."})
        elif best["value"] < 0.1 and game.num_players > 1:
            out.append({"kind": "balance", "text": f"The first player appears to be lost from the opening (value {best['value']:.0%})."})

    if sp and game.num_players > 1:
        avgs = sp["seat_avg_return"]
        hi, lo = max(range(len(avgs)), key=avgs.__getitem__), min(range(len(avgs)), key=avgs.__getitem__)
        gap = avgs[hi] - avgs[lo]
        if gap > 0.15:
            out.append({"kind": "balance", "text": f"Seat advantage: player {hi} averages {avgs[hi]:.0%} vs player {lo} at {avgs[lo]:.0%} in MCTS self-play."})
        else:
            out.append({"kind": "balance", "text": f"Seats look balanced in self-play (spread {gap:.0%} across {sp['games']} games)."})

    if sp:
        common = [a for a in sp["actions"] if a["available"] >= max(8, sp["games"])]
        favored = sorted([a for a in common if a["pick_rate"] >= 0.6], key=lambda a: -a["pick_rate"])[:3]
        ignored = sorted([a for a in common if a["pick_rate"] <= 0.05], key=lambda a: a["pick_rate"])[:3]
        for a in favored:
            out.append({"kind": "strategy", "text": f"**{a['action']}** is taken {a['pick_rate']:.0%} of the time it is available, so it's a core part of the strategy."})
        for a in ignored:
            out.append({"kind": "design", "text": f"**{a['action']}** is almost never chosen ({a['chosen']}/{a['available']} times available). It may be weak or dominated."})
        if sp["timeouts"]:
            out.append({"kind": "design", "text": f"{sp['timeouts']}/{sp['games']} self-play games hit the step cap without ending. Check for stalling strategies."})

    if base:
        delta = base["mcts_avg_return"] - base["random_avg_return"]
        if delta < 0.1:
            out.append({"kind": "design", "text": f"MCTS barely beats random play ({base['mcts_avg_return']:.0%} vs {base['random_avg_return']:.0%}). Outcomes may be dominated by luck, or the search budget is too small."})
        else:
            out.append({"kind": "skill", "text": f"Skill matters: MCTS scores {base['mcts_avg_return']:.0%} against random opponents' {base['random_avg_return']:.0%}."})
    return out


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--params", required=True)
    args = ap.parse_args()
    with open(args.params, encoding="utf-8") as f:
        params = json.load(f)

    clock = Clock(float(params.get("time_budget") or 0))
    try:
        progress("load", 0, "Importing game module")
        game, default_config = load_game(args.repo)
        config = {**default_config, **(params.get("config") or {})}
        max_steps = int(getattr(game, "max_steps", 500))
        if not params.get("rollout_depth"):
            params["rollout_depth"] = int(getattr(game, "rollout_depth", max_steps))

        issues = validate(game, config, max_steps)
        info = {"name": game.name, "num_players": game.num_players,
                "perfect_information": bool(game.perfect_information), "config": config,
                "has_rollout_policy": hasattr(game, "default_rollout_policy")}
        if any(i["severity"] == "error" for i in issues):
            emit({"type": "result", "game": info, "validation": issues, "aborted": True,
                  "insights": [{"kind": "design", "text": "Conformance checks failed, so the analysis was skipped. Fix the errors above and resubmit."}]})
            return

        opening = analyze_opening(game, config, params, max_steps, clock)
        sp = self_play(game, config, params, max_steps, clock)
        base = vs_random(game, config, params, max_steps, clock)
        progress("done", 1, "Finished")
        emit({"type": "result", "game": info, "validation": issues, "opening": opening, "self_play": sp,
              "baseline": base, "insights": build_insights(game, opening, sp, base, params), "params": params})
    except GameError as e:
        emit({"type": "error", "message": str(e), "traceback": traceback.format_exc()})
    except Exception as e:
        emit({"type": "error", "message": f"Unexpected error: {e!r}", "traceback": traceback.format_exc()})


if __name__ == "__main__":
    main()
