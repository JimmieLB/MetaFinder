// Head-to-head games between AI players that value positions differently, run on a thread pool.
// Used by tune.mjs. Each seat gets its own MCTS engine and searches a copy of the state made with
// state.withConfig(seatConfig), so only the seat's *judgment* differs; the game itself is played
// (and, at the turn limit, scored) under the referee config.

import fs from "node:fs";
import os from "node:os";
import { Worker, isMainThread, parentPort, workerData } from "node:worker_threads";
import { CHANCE, MCTS, Rng, actionKey, sampleChance } from "./mcts.mjs";
import { loadGame } from "./load.mjs";

/**
 * Plays one game. `match` = {seed, referee, seats: [{config, iterations, reuseTree}], exploration,
 * rolloutDepth, maxSteps, record}. Returns {returns, steps, finished} where `finished` means the game
 * ended by its own rules rather than the step cap. With `record`, also `moves` (one entry per action, with
 * the position after it) and `initial` (the starting position), for a replay.
 */
export function playMatch(game, match) {
  const s = game.newGame(match.seed, structuredClone(match.referee));
  if (typeof s.withConfig !== "function") {
    throw new Error("Tuning needs state.withConfig(config): a copy of the state that values positions with that config");
  }
  const rng = new Rng(match.seed * 7919 + 1);
  const rollout = typeof game.defaultRolloutPolicy === "function" ? game.defaultRolloutPolicy.bind(game) : null;
  const engines = match.seats.map((seat, p) => new MCTS({
    nPlayers: game.numPlayers,
    perfectInformation: Boolean(game.perfectInformation),
    iterations: seat.iterations,
    exploration: match.exploration,
    maxDepth: match.rolloutDepth,
    rolloutPolicy: rollout,
    rng: new Rng(match.seed * 131 + p),
    reuseTree: seat.reuseTree ?? true,
  }));
  const moves = match.record ? [] : null;
  const initial = match.record ? snapshot(s) : null;
  let steps = 0;
  while (!s.isTerminal() && steps < match.maxSteps) {
    const p = s.currentPlayer();
    let a;
    let value = null;
    if (p === CHANCE) a = sampleChance(s, rng);
    else {
      const view = s.withConfig(structuredClone(match.seats[p].config));
      const [best, root] = engines[p].bestAction(view, p);
      a = best;
      if (match.record) value = root.children.get(actionKey(a))?.mean(p) ?? null;
    }
    const actionLabel = match.record ? label(s, a) : null;
    s.apply(a);
    for (const e of engines) e.advance(a);
    steps += 1;
    if (match.record) {
      moves.push({
        step: steps,
        player: p === CHANCE ? "chance" : p,
        ai: p === CHANCE ? null : match.seats[p].name ?? null,
        action: actionLabel,
        raw_action: JSON.parse(JSON.stringify(a ?? null)),
        value: value == null ? null : Math.round(value * 1000) / 1000,
        ...snapshot(s),
      });
    }
  }
  const finished = s.isTerminal();
  let returns;
  if (finished) returns = Array.from(s.returns());
  else if (typeof s.heuristicValue === "function") returns = Array.from(s.heuristicValue());
  else returns = new Array(game.numPlayers).fill(1 / game.numPlayers);
  return match.record ? { returns, steps, finished, initial, moves } : { returns, steps, finished };
}

const label = (state, a) => (typeof state.actionLabel === "function" ? String(state.actionLabel(a))
  : typeof a === "object" ? JSON.stringify(a) : String(a));

/** The position as the module describes it: describe() text and toJSON() data, when it has them. */
function snapshot(state) {
  const out = {};
  if (typeof state.describe === "function") out.position = String(state.describe());
  // A deep copy: toJSON() may hand out the state's own arrays, which later moves change.
  if (typeof state.toJSON === "function") out.state = JSON.parse(JSON.stringify(state.toJSON()));
  return out;
}

/** Runs matches on worker threads, each of which imports the game module once. */
export class Pool {
  /** heapMb: heap cap per thread, so too many threads fail with a clear error instead of exhausting memory. */
  constructor(repoDir, size = defaultThreads(), { heapMb } = {}) {
    this.size = size;
    this.workers = [];
    this.idle = [];
    this.queue = [];
    for (let i = 0; i < size; i += 1) {
      const w = new Worker(new URL(import.meta.url), {
        workerData: { repoDir },
        stdout: true,
        ...(heapMb ? { resourceLimits: { maxOldGenerationSizeMb: heapMb } } : {}),
      });
      w.stdout.on("data", () => {}); // the game's own logging is dropped
      w.on("message", (msg) => {
        const job = w.job;
        w.job = null;
        if (msg.error) job.reject(new Error(msg.error));
        else job.resolve(msg.result);
        this.next(w);
      });
      w.on("error", (e) => {
        const err = e?.code === "ERR_WORKER_OUT_OF_MEMORY"
          ? new Error(`A game thread ran out of memory (${heapMb ?? "default"} MB heap per thread). ` +
            "Use fewer threads, or give each run more memory.")
          : e;
        if (w.job) w.job.reject(err);
        else for (const job of this.queue.splice(0)) job.reject(err);
        w.job = null;
      });
      this.workers.push(w);
      this.idle.push(w);
    }
  }

  next(w) {
    const job = this.queue.shift();
    if (!job) {
      this.idle.push(w);
      return;
    }
    w.job = job;
    w.postMessage(job.match);
  }

  play(match) {
    return new Promise((resolve, reject) => {
      this.queue.push({ match, resolve, reject });
      const w = this.idle.pop();
      if (w) this.next(w);
    });
  }

  close() {
    return Promise.all(this.workers.map((w) => w.terminate()));
  }
}

export function defaultThreads() {
  const cpus = typeof os.availableParallelism === "function" ? os.availableParallelism() : os.cpus().length;
  return Math.max(1, Math.min(cpus, cpuQuota() ?? Infinity) - 1);
}

/**
 * CPUs allowed by the container's CPU quota (Linux cgroups), or null if unlimited. Containers (Railway,
 * Docker --cpus) usually see every core of the host but may only use a few; os.availableParallelism()
 * doesn't know that.
 */
export function cpuQuota() {
  const read = (f) => {
    try {
      return fs.readFileSync(f, "utf8").trim();
    } catch {
      return null;
    }
  };
  const v2 = read("/sys/fs/cgroup/cpu.max"); // "max 100000" or "<quota> <period>"
  if (v2) {
    const [quota, period] = v2.split(/\s+/);
    if (quota !== "max" && Number(quota) > 0 && Number(period) > 0) return Math.max(1, Math.ceil(Number(quota) / Number(period)));
    return null;
  }
  const quota = Number(read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"));
  const period = Number(read("/sys/fs/cgroup/cpu/cpu.cfs_period_us"));
  return quota > 0 && period > 0 ? Math.max(1, Math.ceil(quota / period)) : null;
}

// ---------------------------------------------------------------------------- thread entry
if (!isMainThread && workerData?.repoDir) {
  const loaded = loadGame(workerData.repoDir);
  parentPort.on("message", async (match) => {
    try {
      const [game] = await loaded;
      parentPort.postMessage({ result: playMatch(game, match) });
    } catch (e) {
      parentPort.postMessage({ error: e?.stack || String(e) });
    }
  });
}
