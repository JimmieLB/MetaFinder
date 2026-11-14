// Head-to-head games between AI players that value positions differently, run on a thread pool.
// Used by tune.mjs. Each seat gets its own MCTS engine and searches a copy of the state made with
// state.withConfig(seatConfig), so only the seat's *judgment* differs; the game itself is played
// (and, at the turn limit, scored) under the referee config.

import os from "node:os";
import { Worker, isMainThread, parentPort, workerData } from "node:worker_threads";
import { CHANCE, MCTS, Rng, sampleChance } from "./mcts.mjs";
import { loadGame } from "./load.mjs";

/**
 * Plays one game. `match` = {seed, referee, seats: [{config, iterations, reuseTree}], exploration,
 * rolloutDepth, maxSteps}. Returns {returns, steps, finished} where `finished` means the game ended
 * by its own rules rather than the step cap.
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
  let steps = 0;
  while (!s.isTerminal() && steps < match.maxSteps) {
    const p = s.currentPlayer();
    let a;
    if (p === CHANCE) a = sampleChance(s, rng);
    else {
      const view = s.withConfig(structuredClone(match.seats[p].config));
      a = engines[p].bestAction(view, p)[0];
    }
    s.apply(a);
    for (const e of engines) e.advance(a);
    steps += 1;
  }
  const finished = s.isTerminal();
  let returns;
  if (finished) returns = Array.from(s.returns());
  else if (typeof s.heuristicValue === "function") returns = Array.from(s.heuristicValue());
  else returns = new Array(game.numPlayers).fill(1 / game.numPlayers);
  return { returns, steps, finished };
}

/** Runs matches on worker threads, each of which imports the game module once. */
export class Pool {
  constructor(repoDir, size = defaultThreads()) {
    this.size = size;
    this.workers = [];
    this.idle = [];
    this.queue = [];
    for (let i = 0; i < size; i += 1) {
      const w = new Worker(new URL(import.meta.url), { workerData: { repoDir }, stdout: true });
      w.stdout.on("data", () => {}); // the game's own logging is dropped
      w.on("message", (msg) => {
        const job = w.job;
        w.job = null;
        if (msg.error) job.reject(new Error(msg.error));
        else job.resolve(msg.result);
        this.next(w);
      });
      w.on("error", (e) => {
        if (w.job) w.job.reject(e);
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
  return Math.max(1, cpus - 1);
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
