// Analysis worker for games written in JavaScript: a port of metafinder/worker.py.
//
// Usage: node metafinder/node/worker.mjs --repo DIR --params params.json
//
// Same protocol and result format as the Python worker: one JSON object per line on stdout
//   {"type": "progress", "phase", "pct", "message"} | {"type": "result", ...} | {"type": "error", "message", "traceback"}
// Anything the game prints (console.log, process.stdout.write) goes to stderr so it cannot corrupt the protocol.

import fs from "node:fs";
import { CHANCE, MCTS, Rng, actionKey, proofStatus, rankChildren, sampleChance } from "./mcts.mjs";
import { GameError, loadGame } from "./load.mjs";

// ---------------------------------------------------------------------------- protocol output
function writeOut(text) {
  const buf = Buffer.from(text, "utf8");
  let off = 0;
  while (off < buf.length) {
    try {
      off += fs.writeSync(1, buf, off);
    } catch (e) {
      if (e.code !== "EAGAIN") throw e; // non-blocking pipe is full: retry
    }
  }
}
const toStderr = (...args) => process.stderr.write(args.map((a) => (typeof a === "string" ? a : inspectSafe(a))).join(" ") + "\n");
const inspectSafe = (a) => {
  try {
    return JSON.stringify(a);
  } catch {
    return String(a);
  }
};
console.log = console.info = console.debug = toStderr;
process.stdout.write = process.stderr.write.bind(process.stderr);

function emit(obj) {
  writeOut(JSON.stringify(obj, (_k, v) => (typeof v === "bigint" ? String(v) : v)) + "\n");
}

function progress(phase, pct, message = "") {
  emit({ type: "progress", phase, pct: Math.round(pct * 1000) / 1000, message });
}

// ---------------------------------------------------------------------------- helpers
const label = (state, a) => (typeof state.actionLabel === "function" ? String(state.actionLabel(a)) : typeof a === "object" ? JSON.stringify(a) : String(a));
const describe = (state) => (typeof state.describe === "function" ? String(state.describe()) : null);
const drain = (state) => (typeof state.drainEvents === "function" ? state.drainEvents() ?? [] : []);
const copyConfig = (config) => structuredClone(config);

function stableStringify(v) {
  if (v === null || typeof v !== "object") return JSON.stringify(v) ?? "null";
  if (typeof v.toJSON === "function" && !Array.isArray(v)) return stableStringify(v.toJSON());
  if (Array.isArray(v)) return `[${v.map(stableStringify).join(",")}]`;
  if (v instanceof Map) return stableStringify(Object.fromEntries(v));
  if (v instanceof Set) return stableStringify([...v]);
  return `{${Object.keys(v).sort().filter((k) => v[k] !== undefined).map((k) => `${JSON.stringify(k)}:${stableStringify(v[k])}`).join(",")}}`;
}

function fingerprint(state) {
  if (typeof state.toJSON === "function") return stableStringify(state.toJSON());
  if (state.isTerminal()) return stableStringify(["terminal", Array.from(state.returns())]);
  const p = state.currentPlayer();
  if (p === CHANCE) return '["chance"]';
  return stableStringify([p, state.legalActions().map(actionKey)]);
}

function eventKey(ev) {
  const parts = [String(ev.type ?? "event")];
  for (const k of Object.keys(ev).sort()) {
    const v = ev[k];
    if (["type", "player", "turn", "tick", "t", "step"].includes(k) || (v !== null && typeof v === "object")) continue;
    parts.push(`${k}=${v}`);
  }
  return parts.join(" · ");
}

class Counter extends Map {
  add(key, n = 1) {
    this.set(key, (this.get(key) ?? 0) + n);
  }
  update(other) {
    for (const [k, v] of other) this.add(k, v);
  }
  mostCommon(n = Infinity) {
    return [...this.entries()].sort((a, b) => b[1] - a[1]).slice(0, n);
  }
  total() {
    let t = 0;
    for (const v of this.values()) t += v;
    return t;
  }
}

const now = () => performance.now() / 1000;

// ---------------------------------------------------------------------------- phase 1: conformance
function validate(game, config, maxSteps, nGames = 6) {
  const issues = [];
  const rng = new Rng(1234);
  const n = game.numPlayers;
  const add = (severity, message) => {
    if (!issues.some((i) => i.message === message)) issues.push({ severity, message });
  };

  for (let g = 0; g < nGames; g += 1) {
    progress("validate", g / nGames, `Random playthrough ${g + 1}/${nGames}`);
    let s;
    try {
      s = game.newGame(g, copyConfig(config));
    } catch (e) {
      throw new GameError(`newGame() crashed: ${e?.stack || e}`);
    }
    const history = [];
    let steps = 0;
    try {
      while (!s.isTerminal() && steps < maxSteps) {
        const p = s.currentPlayer();
        let a;
        if (p === CHANCE) {
          if (typeof s.chanceOutcomes === "function") {
            const pairs = s.chanceOutcomes();
            if (!pairs?.length) {
              add("error", "chanceOutcomes() returned no outcomes");
              break;
            }
            const total = pairs.reduce((t, [, prob]) => t + prob, 0);
            if (Math.abs(total - 1) > 1e-6) add("warning", `Chance probabilities sum to ${total.toFixed(4)}, not 1`);
          } else if (typeof s.sampleChance !== "function") {
            throw new GameError("State reached a CHANCE node but has neither chanceOutcomes() nor sampleChance()");
          }
          a = sampleChance(s, rng);
        } else {
          if (!(Number.isInteger(p) && p >= 0 && p < n)) {
            add("error", `currentPlayer() returned ${p}, expected 0..${n - 1} or -1 (CHANCE)`);
            break;
          }
          const legal = Array.from(s.legalActions());
          if (!legal.length) {
            add("error", "A non-terminal state has no legal actions");
            break;
          }
          const keys = legal.map(actionKey);
          if (new Set(keys).size !== keys.length) add("warning", "legalActions() contains duplicates");
          if (steps % 4 === 0) {
            const before = fingerprint(s);
            const c = s.clone();
            c.apply(legal[legal.length - 1]);
            if (fingerprint(s) !== before) add("error", "clone() is not independent: applying an action to the clone changed the original");
            if (!game.perfectInformation) {
              if (typeof s.determinize !== "function") throw new GameError("perfectInformation is false but the state has no determinize()");
              const d = s.determinize(p, rng);
              const dKeys = Array.from(d.legalActions()).map(actionKey).sort();
              const same = d.currentPlayer() === p && dKeys.length === new Set(keys).size && [...new Set(keys)].sort().every((k, i) => k === dKeys[i]);
              if (!same) add("warning", "determinize() changed the observer's own legal actions (it may be leaking or losing information)");
              if (fingerprint(s) !== before) add("error", "determinize() mutated the original state; it must return a new state");
            }
          }
          a = rng.choice(legal);
        }
        s.apply(a);
        history.push(a);
        drain(s);
        steps += 1;
      }
    } catch (e) {
      if (e instanceof GameError) throw e;
      throw new GameError(`Game crashed during a random playthrough at step ${steps}: ${e?.stack || e}`);
    }

    if (steps >= maxSteps) {
      add("warning", `A random playthrough did not finish within ${maxSteps} steps (treated as a timeout)`);
    } else {
      const r = Array.from(s.returns());
      if (r.length !== n) add("error", `returns() has ${r.length} values, expected ${n}`);
      else if (r.some((v) => !(v >= -1e-9 && v <= 1 + 1e-9))) add("warning", `returns() should be in [0, 1], got ${JSON.stringify(r)}`);
    }

    const replay = game.newGame(g, copyConfig(config));
    for (const a of history) replay.apply(a);
    if (fingerprint(replay) !== fingerprint(s)) {
      add("error", "Replaying the same seed and actions gave a different state (hidden randomness? use CHANCE nodes or the seed)");
    }
  }

  if (!issues.length) issues.push({ severity: "ok", message: `${nGames} random playthroughs passed all checks` });
  return issues;
}

// ---------------------------------------------------------------------------- phase 2: opening analysis
function makeEngine(game, params, rng, iterations = null) {
  const rollout = typeof game.defaultRolloutPolicy === "function" ? game.defaultRolloutPolicy.bind(game) : null;
  return new MCTS({
    nPlayers: game.numPlayers,
    perfectInformation: Boolean(game.perfectInformation),
    iterations: iterations || params.iterations,
    exploration: params.exploration,
    maxDepth: params.rollout_depth,
    rolloutPolicy: (params.use_module_rollout ?? true) ? rollout : null,
    rng,
    reuseTree: params.tree_reuse ?? true,
  });
}

function legalChildren(root, state) {
  const out = [];
  for (const a of state.legalActions()) {
    const c = root.children.get(actionKey(a));
    if (c) out.push(c);
  }
  return out;
}

function analyzeOpening(game, config, params, maxSteps, clock) {
  const rng = new Rng(params.seed);
  const s = game.newGame(params.seed, copyConfig(config));
  let steps = 0;
  while (s.currentPlayer() === CHANCE && !s.isTerminal() && steps < maxSteps) {
    s.apply(sampleChance(s, rng));
    steps += 1;
  }
  drain(s);
  if (s.isTerminal()) return null;
  const player = s.currentPlayer();

  const total = params.opening_iterations;
  const engine = makeEngine(game, params, rng, total);
  if (clock.budget !== null) engine.timeLimit = clock.budget * 0.1; // leave the rest for self-play
  const msg = `Deep search from the opening position (${total.toLocaleString("en-US")} iterations)`;
  const t0 = now();
  const root = engine.search(s, player, (i) => progress("opening", i / total, msg));
  const elapsed = now() - t0;

  const moves = rankChildren(legalChildren(root, s), player).map((c) => ({
    action: label(s, c.action),
    visits: c.visits,
    share: c.visits / Math.max(1, root.visits),
    value: c.mean(player),
    proven: proofStatus(c, player),
  }));

  // Principal variation: follow the most-visited child.
  const pv = [];
  let node = root;
  const walk = game.perfectInformation ? s.clone() : s.determinize(player, rng);
  while (node.children.size && pv.length < 14) {
    let child;
    if (node.toMove !== null && node.toMove !== CHANCE) child = rankChildren(node.children.values(), node.toMove)[0];
    else child = [...node.children.values()].reduce((a, b) => (b.visits > a.visits ? b : a));
    if (child.visits < 8 && child.proven === null) break;
    const isChance = child.player === CHANCE;
    if (!isChance && !walk.legalActions().some((a) => actionKey(a) === actionKey(child.action))) break;
    pv.push({
      player: isChance ? "chance" : child.player,
      action: label(walk, child.action),
      value: isChance ? null : child.mean(child.player),
      visits: child.visits,
    });
    walk.apply(child.action);
    node = child;
    if (walk.isTerminal()) break;
  }

  return {
    player,
    position: describe(s),
    iterations: root.visits,
    seconds: elapsed,
    iterations_per_second: root.visits / Math.max(1e-6, elapsed),
    moves,
    principal_variation: pv,
    proven: proofStatus(root, player),
    cut: root.visits < total && root.proven === null,
  };
}

// ---------------------------------------------------------------------------- phase 3/4: games
/**
 * seatAgents[p] is an MCTS engine or null (uniform random). If the clock passes `stopAt` (seconds,
 * see now()) the game is cut short and scored like a step-cap timeout, with `cut` set.
 */
function playGame(game, config, seed, seatAgents, maxSteps, record = false, stats = null, stopAt = null) {
  const rng = new Rng(seed * 7919 + 1);
  const s = game.newGame(seed, copyConfig(config));
  let steps = 0;
  let cut = false;
  const transcript = [];
  const events = new Counter();
  const hasKey = typeof s.strategyKey === "function";
  const engines = [...new Set(seatAgents.filter(Boolean))];
  for (const e of engines) e.resetTree?.();
  while (!s.isTerminal() && steps < maxSteps) {
    if (stopAt !== null && now() > stopAt) {
      cut = true;
      break;
    }
    const p = s.currentPlayer();
    let a;
    if (p === CHANCE) {
      a = sampleChance(s, rng);
      if (record && transcript.length < 200) transcript.push({ player: "chance", action: label(s, a) });
    } else {
      const agent = seatAgents[p];
      const legal = Array.from(s.legalActions());
      if (!agent) {
        a = rng.choice(legal);
      } else {
        const pos = record ? describe(s) : null;
        const key = stats && hasKey ? String(s.strategyKey()) : null;
        let root;
        [a, root] = agent.bestAction(s, p);
        if (stats) {
          const lab = label(s, a);
          for (const x of legal) actionStats(stats, label(s, x), game.numPlayers).available += 1;
          const st = actionStats(stats, lab, game.numPlayers);
          st.chosen += 1;
          st.by_seat[p] += 1;
          if (key !== null) {
            counterOf(stats.policy, key).add(lab);
            counterOf(stats.policyProof, key).add(proofStatus(root, p) ?? "unsolved");
          }
        }
        if (record && transcript.length < 200) {
          const legalKeys = new Set(legal.map(actionKey));
          const chosenKey = actionKey(a);
          const ranked = [...root.children.values()].sort((x, y) => y.visits - x.visits).slice(0, 3);
          transcript.push({
            player: p,
            position: pos,
            action: label(s, a),
            value: root.children.get(chosenKey)?.mean(p) ?? null,
            alternatives: ranked
              .filter((c) => actionKey(c.action) !== chosenKey && legalKeys.has(actionKey(c.action)))
              .slice(0, 2)
              .map((c) => ({ action: label(s, c.action), share: c.visits / Math.max(1, root.visits), value: c.mean(p) })),
          });
        }
      }
    }
    s.apply(a);
    for (const e of engines) e.advance?.(a); // keeps each engine's tree in step with the game
    for (const ev of drain(s)) events.add(eventKey(ev));
    steps += 1;
  }
  const timedOut = !s.isTerminal();
  let returns;
  if (timedOut) {
    returns = typeof s.heuristicValue === "function" ? Array.from(s.heuristicValue()) : new Array(game.numPlayers).fill(1 / game.numPlayers);
  } else {
    returns = Array.from(s.returns());
  }
  return { returns, steps, timedOut: timedOut && !cut, cut, events, transcript, final: describe(s) };
}

/**
 * Time budget for one analysis, so a job finishes with partial results instead of being killed.
 * `soft` = don't start another game after this; `hard` = cut the game in progress.
 */
class Clock {
  constructor(budget) {
    this.start = now();
    this.budget = budget > 0 ? budget : null;
  }

  at(fraction) {
    return this.budget === null ? null : this.start + this.budget * fraction;
  }

  /** Whether a game expected to take `perGame` seconds can still start before `soft`. */
  canStart(soft, perGame) {
    return soft === null || now() + perGame <= soft;
  }
}

function actionStats(stats, name, n) {
  let st = stats.actions.get(name);
  if (!st) stats.actions.set(name, (st = { available: 0, chosen: 0, by_seat: new Array(n).fill(0) }));
  return st;
}

function counterOf(map, key) {
  let c = map.get(key);
  if (!c) map.set(key, (c = new Counter()));
  return c;
}

function selfPlay(game, config, params, maxSteps, clock) {
  const requested = params.games;
  const soft = clock.at(0.7);
  const hard = clock.at(0.85);
  const n = game.numPlayers;
  const stats = { actions: new Map(), policy: new Map(), policyProof: new Map() };
  const seatReturns = Array.from({ length: n }, () => []);
  const lengths = [];
  let timeouts = 0;
  const events = new Counter();
  let sample = null;
  let cut = 0;
  const t0 = now();
  for (let g = 0; g < requested; g += 1) {
    const perGame = g ? (now() - t0) / g : 0;
    if (g && !clock.canStart(soft, perGame)) break; // out of time: report the games played so far
    const eta = g ? ` · ~${(perGame * (requested - g)).toFixed(0)}s left` : "";
    progress("selfplay", g / requested, `MCTS vs MCTS, game ${g + 1}/${requested}${eta}`);
    const agents = Array.from({ length: n }, (_, p) => makeEngine(game, params, new Rng(params.seed + 1000 * g + p)));
    const res = playGame(game, config, params.seed + g, agents, maxSteps, g === 0, stats, hard);
    for (let p = 0; p < n; p += 1) seatReturns[p].push(res.returns[p]);
    lengths.push(res.steps);
    timeouts += res.timedOut ? 1 : 0;
    cut += res.cut ? 1 : 0;
    events.update(res.events);
    if (g === 0) sample = { moves: res.transcript, returns: res.returns, final: res.final, timed_out: res.timedOut || res.cut };
  }
  const nGames = lengths.length;
  clock.selfPlayPerGame = (now() - t0) / nGames;

  const actions = [...stats.actions.entries()].map(([action, a]) => ({
    action,
    available: a.available,
    chosen: a.chosen,
    pick_rate: a.available ? a.chosen / a.available : 0,
    by_seat: a.by_seat,
  }));
  actions.sort((x, y) => y.chosen - x.chosen || y.available - x.available);

  const policy = [];
  for (const [key, counter] of stats.policy) {
    const total = counter.total();
    const [best, cnt] = counter.mostCommon(1)[0];
    const proof = stats.policyProof.get(key);
    const top = proof?.mostCommon(1)[0];
    const solved = top && top[1] === total ? top[0] : null;
    policy.push({
      situation: key,
      best_action: best,
      confidence: cnt / total,
      samples: total,
      solved: solved === "unsolved" ? null : solved,
      others: counter.mostCommon(4).slice(1).map(([action, count]) => ({ action, count })),
    });
  }
  policy.sort((a, b) => naturalCompare(a.situation, b.situation));

  return {
    games: nGames,
    games_requested: requested,
    cut_games: cut,
    seat_avg_return: seatReturns.map(avg),
    seat_ci95: seatReturns.map(ci95),
    avg_length: avg(lengths),
    min_length: Math.min(...lengths),
    max_length: Math.max(...lengths),
    timeouts,
    actions,
    policy,
    events: events.mostCommon(40).map(([event, count]) => ({ event, count, per_game: count / nGames })),
    sample_game: sample,
    seconds: now() - t0,
  };
}

function vsRandom(game, config, params, maxSteps, clock) {
  const n = game.numPlayers;
  const requested = Math.max(2, Math.floor(params.games / 2));
  const soft = clock.at(0.95);
  const hard = clock.at(0.97);
  const mctsScores = [];
  const randomScores = [];
  const t0 = now();
  for (let g = 0; g < requested; g += 1) {
    // An MCTS-vs-random game costs about 1/n of a self-play game, so use that as the first estimate.
    const perGame = g ? (now() - t0) / g : clock.selfPlayPerGame / n;
    if (!clock.canStart(soft, perGame)) break;
    progress("baseline", g / requested, `MCTS vs random, game ${g + 1}/${requested}`);
    const seat = g % n;
    const agents = new Array(n).fill(null);
    agents[seat] = makeEngine(game, params, new Rng(params.seed + 50_000 + g));
    const res = playGame(game, config, params.seed + 10_000 + g, agents, maxSteps, false, null, hard);
    if (res.cut) break; // a half-played game says little about skill
    mctsScores.push(res.returns[seat]);
    if (n === 1) {
      randomScores.push(playGame(game, config, params.seed + 10_000 + g, [null], maxSteps).returns[0]);
    } else {
      const others = res.returns.filter((_, p) => p !== seat);
      randomScores.push(avg(others));
    }
  }
  if (!mctsScores.length) return null;
  return { games: mctsScores.length, games_requested: requested, mcts_avg_return: avg(mctsScores), random_avg_return: avg(randomScores) };
}

const avg = (xs) => xs.reduce((t, x) => t + x, 0) / xs.length;

function ci95(xs) {
  if (xs.length < 2) return 0;
  const m = avg(xs);
  const v = xs.reduce((t, x) => t + (x - m) ** 2, 0) / (xs.length - 1);
  return 1.96 * Math.sqrt(v / xs.length);
}

function naturalCompare(a, b) {
  const pa = a.split(/(\d+)/);
  const pb = b.split(/(\d+)/);
  for (let i = 0; i < Math.min(pa.length, pb.length); i += 1) {
    if (pa[i] === pb[i]) continue;
    const na = /^\d+$/.test(pa[i]);
    const nb = /^\d+$/.test(pb[i]);
    if (na && nb) return Number(pa[i]) - Number(pb[i]);
    return pa[i] < pb[i] ? -1 : 1;
  }
  return pa.length - pb.length;
}

// ---------------------------------------------------------------------------- insights
const pct = (v) => `${Math.round(v * 100)}%`;

function buildInsights(game, opening, sp, base, params) {
  const out = [];
  const short = [];
  if (opening?.cut) short.push(`the opening search stopped at ${opening.iterations.toLocaleString("en-US")} of ${params.opening_iterations.toLocaleString("en-US")} iterations`);
  if (sp && sp.games < sp.games_requested) short.push(`${sp.games} of ${sp.games_requested} self-play games were played`);
  if (sp?.cut_games) short.push(`${sp.cut_games} self-play game(s) were cut short and scored by heuristic`);
  if (!base) short.push("the skill check against random players was skipped");
  else if (base.games < base.games_requested) short.push(`${base.games} of ${base.games_requested} skill-check games were played`);
  if (short.length) {
    out.push({
      kind: "design",
      text: `**Time budget reached** (${Math.round(params.time_budget)}s): ${short.join("; ")}. Results are less reliable. ` +
        "Lower iterations per move or max_turns, or raise METAFINDER_JOB_TIMEOUT.",
    });
  }
  if (opening && opening.moves.length) {
    const best = opening.moves[0];
    let line = `Best opening for player ${opening.player}: **${best.action}**, estimated win value ${pct(best.value)}`;
    const losing = opening.moves.slice(1).filter((m) => m.proven === "loss");
    if (best.proven === "win") {
      line = `Solved: **${best.action}** is a forced win for player ${opening.player} from the opening.`;
    } else if (losing.length && losing.length === opening.moves.length - 1) {
      line = `Best opening for player ${opening.player}: **${best.action}**. Every alternative ` +
        `(${losing.map((m) => m.action).join(", ")}) is a proven forced loss.`;
    } else if (opening.proven === "loss") {
      line = `Solved: player ${opening.player} loses against perfect play from the opening, whatever they do.`;
    } else if (opening.moves.length > 1) {
      const second = opening.moves[1];
      line += ` (next best: ${second.action} at ${pct(second.value)}`;
      line += best.value - second.value > 0.1 ? ", a clear favourite)" : ", close call)";
    }
    out.push({ kind: "strategy", text: line });
    if (best.value > 0.9) {
      out.push({ kind: "balance", text: `The first player appears to have a forced or near-forced win from the opening (value ${pct(best.value)}).` });
    } else if (best.value < 0.1 && game.numPlayers > 1) {
      out.push({ kind: "balance", text: `The first player appears to be lost from the opening (value ${pct(best.value)}).` });
    }
  }

  if (sp && game.numPlayers > 1) {
    const avgs = sp.seat_avg_return;
    const hi = avgs.indexOf(Math.max(...avgs));
    const lo = avgs.indexOf(Math.min(...avgs));
    const gap = avgs[hi] - avgs[lo];
    if (gap > 0.15) out.push({ kind: "balance", text: `Seat advantage: player ${hi} averages ${pct(avgs[hi])} vs player ${lo} at ${pct(avgs[lo])} in MCTS self-play.` });
    else out.push({ kind: "balance", text: `Seats look balanced in self-play (spread ${pct(gap)} across ${sp.games} games).` });
  }

  if (sp) {
    const common = sp.actions.filter((a) => a.available >= Math.max(8, sp.games));
    const favored = common.filter((a) => a.pick_rate >= 0.6).sort((a, b) => b.pick_rate - a.pick_rate).slice(0, 3);
    const ignored = common.filter((a) => a.pick_rate <= 0.05).sort((a, b) => a.pick_rate - b.pick_rate).slice(0, 3);
    for (const a of favored) out.push({ kind: "strategy", text: `**${a.action}** is taken ${pct(a.pick_rate)} of the time it is available, so it's a core part of the strategy.` });
    for (const a of ignored) out.push({ kind: "design", text: `**${a.action}** is almost never chosen (${a.chosen}/${a.available} times available). It may be weak or dominated.` });
    if (sp.timeouts) out.push({ kind: "design", text: `${sp.timeouts}/${sp.games} self-play games hit the step cap without ending. Check for stalling strategies.` });
  }

  if (base) {
    if (base.mcts_avg_return - base.random_avg_return < 0.1) {
      out.push({ kind: "design", text: `MCTS barely beats random play (${pct(base.mcts_avg_return)} vs ${pct(base.random_avg_return)}). Outcomes may be dominated by luck, or the search budget is too small.` });
    } else {
      out.push({ kind: "skill", text: `Skill matters: MCTS scores ${pct(base.mcts_avg_return)} against random opponents' ${pct(base.random_avg_return)}.` });
    }
  }
  return out;
}

// ---------------------------------------------------------------------------- main
function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--repo") out.repo = argv[++i];
    else if (argv[i] === "--params") out.params = argv[++i];
  }
  if (!out.repo || !out.params) {
    process.stderr.write("usage: node worker.mjs --repo DIR --params params.json\n");
    process.exit(2);
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const params = JSON.parse(fs.readFileSync(args.params, "utf8"));
  const clock = new Clock(Number(params.time_budget) || 0);
  try {
    progress("load", 0, "Importing game module (Node.js)");
    const [game, defaultConfig] = await loadGame(args.repo);
    const config = { ...defaultConfig, ...(params.config ?? {}) };
    const maxSteps = Number.parseInt(game.maxSteps ?? 500, 10);
    if (!params.rollout_depth) params.rollout_depth = Number.parseInt(game.rolloutDepth ?? maxSteps, 10);

    const issues = validate(game, config, maxSteps);
    const info = {
      name: game.name, num_players: game.numPlayers, perfect_information: Boolean(game.perfectInformation), config,
      has_rollout_policy: typeof game.defaultRolloutPolicy === "function", runtime: `node ${process.version}`,
    };
    if (issues.some((i) => i.severity === "error")) {
      emit({
        type: "result", game: info, validation: issues, aborted: true,
        insights: [{ kind: "design", text: "Conformance checks failed, so the analysis was skipped. Fix the errors above and resubmit." }],
      });
      return;
    }

    const opening = analyzeOpening(game, config, params, maxSteps, clock);
    const sp = selfPlay(game, config, params, maxSteps, clock);
    const base = vsRandom(game, config, params, maxSteps, clock);
    progress("done", 1, "Finished");
    emit({ type: "result", game: info, validation: issues, opening, self_play: sp, baseline: base, insights: buildInsights(game, opening, sp, base, params), params });
  } catch (e) {
    const message = e instanceof GameError ? e.message : `Unexpected error: ${e?.message ?? e}`;
    emit({ type: "error", message, traceback: e?.stack ?? String(e) });
  }
}

await main();
