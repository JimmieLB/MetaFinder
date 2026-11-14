// Tunes a game's AI-scoring config by self-play (SPSA), so each run makes the AI a bit stronger.
//
// Usage: node metafinder/node/tune.mjs --repo DIR [options]      (run with --help for the options)
//
// Each round nudges every tuned value up or down at random (a "+" and a "−" version of the current
// settings), plays the two versions against each other with seats swapped, and moves the settings
// toward whichever side scored better. At the end the tuned settings play the starting settings to
// check the result; --write saves them into metafinder.json when they win clearly, so the next run
// starts from there.
//
// What gets tuned: the "tune" list in metafinder.json (or --param), e.g.
//   "tune": ["unit_value_scale", "objective_values.hq", {"path": "objective_values.city", "min": 500, "max": 8000}]
// Values must be positive numbers; they change by percentages (tuned on a log scale).
// The module's state needs withConfig(config): a copy that values positions with that config.
//
// Who judges a game that hits the turn limit: the referee config, which is the base config with the
// tuned keys removed (so the module's defaults), unless --referee overrides it. A fixed referee keeps
// the tuner from just agreeing with itself.

import fs from "node:fs";
import path from "node:path";
import { Pool, defaultThreads } from "./arena.mjs";
import { loadGame } from "./load.mjs";

const HELP = `Tunes a game's AI-scoring values by self-play.

  node metafinder/node/tune.mjs --repo DIR [options]

  --repo DIR          game repository with metafinder.json (required)
  --param PATH        a config value to tune, e.g. objective_values.hq (repeatable; default: manifest "tune")
  --rounds N          tuning rounds (default 30)
  --games N           games per round, seats swapped in pairs (default: threads, made even)
  --iterations N      MCTS iterations per move during tuning (default 150)
  --exploration X     UCT exploration constant (default 0.7)
  --check-games N     games for the final tuned-vs-start check (default 2 × games)
  --threads N         worker threads (default: CPU cores − 1)
  --config JSON       rule overrides merged into the manifest config, e.g. '{"max_turns": 25}'
  --referee JSON      config used to score games that hit the turn limit (default: base config minus tuned keys)
  --out FILE          checkpoint/result file (default: DIR/metafinder.tuned.json)
  --resume            continue from the checkpoint in --out
  --write             save the tuned values into metafinder.json if they beat the start clearly
  --seed N            random seed (default: time-based)
`;

// ---------------------------------------------------------------------------- args
function parseArgs(argv) {
  const o = { params: [] };
  const num = (v, name) => {
    const n = Number(v);
    if (!Number.isFinite(n)) fail(`${name} needs a number, got ${v}`);
    return n;
  };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    const v = () => {
      if (i + 1 >= argv.length) fail(`${a} needs a value`);
      return argv[++i];
    };
    if (a === "--help" || a === "-h") {
      process.stdout.write(HELP);
      process.exit(0);
    } else if (a === "--repo") o.repo = v();
    else if (a === "--param") o.params.push(v());
    else if (a === "--rounds") o.rounds = num(v(), a);
    else if (a === "--games") o.games = num(v(), a);
    else if (a === "--iterations") o.iterations = num(v(), a);
    else if (a === "--exploration") o.exploration = num(v(), a);
    else if (a === "--check-games") o.checkGames = num(v(), a);
    else if (a === "--threads") o.threads = num(v(), a);
    else if (a === "--config") o.config = parseJson(v(), a);
    else if (a === "--referee") o.referee = parseJson(v(), a);
    else if (a === "--out") o.out = v();
    else if (a === "--resume") o.resume = true;
    else if (a === "--write") o.write = true;
    else if (a === "--seed") o.seed = num(v(), a);
    else fail(`Unknown option ${a} (see --help)`);
  }
  if (!o.repo) fail("--repo is required (see --help)");
  return o;
}

function parseJson(text, name) {
  try {
    return JSON.parse(text);
  } catch (e) {
    fail(`${name} is not valid JSON: ${e.message}`);
  }
}

function fail(msg) {
  process.stderr.write(`tune: ${msg}\n`);
  process.exit(2);
}

// ---------------------------------------------------------------------------- config paths
const getPath = (obj, p) => p.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);

function setPath(obj, p, value) {
  const keys = p.split(".");
  let o = obj;
  for (const k of keys.slice(0, -1)) {
    if (o[k] == null || typeof o[k] !== "object") o[k] = {};
    o = o[k];
  }
  o[keys.at(-1)] = value;
}

function deletePath(obj, p) {
  const keys = p.split(".");
  const parent = keys.length > 1 ? getPath(obj, keys.slice(0, -1).join(".")) : obj;
  if (parent && typeof parent === "object") delete parent[keys.at(-1)];
}

function numericLeaves(obj, prefix = "") {
  const out = [];
  for (const [k, v] of Object.entries(obj ?? {})) {
    const p = prefix ? `${prefix}.${k}` : k;
    if (typeof v === "number" && v > 0) out.push(p);
    else if (v && typeof v === "object" && !Array.isArray(v)) out.push(...numericLeaves(v, p));
  }
  return out;
}

/** Tuning specs: [{path, start, min, max, integer}] with positive bounds. */
function tuneSpecs(raw, config) {
  if (!raw.length) {
    fail("Nothing to tune. Add a \"tune\" list to metafinder.json or pass --param. Pick values that only change how " +
      "the AI judges positions, not the rules. Numeric values in your config: " + (numericLeaves(config).join(", ") || "none"));
  }
  return raw.map((item) => {
    const spec = typeof item === "string" ? { path: item } : { ...item };
    if (typeof spec.path !== "string") fail(`Bad tune entry ${JSON.stringify(item)}: needs a "path"`);
    const start = getPath(config, spec.path);
    if (typeof start !== "number" || !(start > 0)) {
      fail(`${spec.path} must be set to a positive number in the config to be tuned (it is ${JSON.stringify(start)})`);
    }
    spec.start = start;
    spec.min = spec.min ?? start / 4;
    spec.max = spec.max ?? start * 4;
    if (!(spec.min > 0 && spec.max >= spec.min && start >= spec.min && start <= spec.max)) {
      fail(`${spec.path}: need 0 < min ≤ start ≤ max (got ${spec.min}, ${start}, ${spec.max})`);
    }
    spec.integer = Number.isInteger(start) && start >= 10;
    return spec;
  });
}

/** A config with the tuned values from theta (log space), rounded for readability. */
function configFor(base, specs, theta) {
  const c = structuredClone(base);
  specs.forEach((s, i) => setPath(c, s.path, roundValue(Math.exp(theta[i]), s.integer)));
  return c;
}

function roundValue(v, integer) {
  if (integer) {
    const unit = 10 ** Math.max(0, Math.floor(Math.log10(v)) - 2); // three significant digits
    return Math.round(v / unit) * unit;
  }
  return Math.round(v * 1000) / 1000;
}

// ---------------------------------------------------------------------------- random
function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ---------------------------------------------------------------------------- matches
/**
 * Plays `games` games of config A against config B (B fills the other seats), rotating A through
 * every seat. Returns A's scores, each in [0, 1] (its share of the game's returns).
 */
async function duel(pool, ctx, configA, configB, games, seedBase) {
  const n = ctx.numPlayers;
  const jobs = [];
  for (let g = 0; g < games; g += 1) {
    const seat = g % n;
    const seed = seedBase + Math.floor(g / n);
    const seats = Array.from({ length: n }, (_, p) => ({
      config: p === seat ? configA : configB,
      iterations: ctx.iterations,
    }));
    jobs.push(pool.play({ ...ctx.match, seed, seats }).then((r) => {
      const total = r.returns.reduce((a, b) => a + b, 0);
      return { score: total > 0 ? r.returns[seat] / total : 1 / n, finished: r.finished };
    }));
  }
  return Promise.all(jobs);
}

const mean = (xs) => xs.reduce((a, b) => a + b, 0) / Math.max(1, xs.length);
const stderr = (xs) => {
  if (xs.length < 2) return 0;
  const m = mean(xs);
  return Math.sqrt(xs.reduce((a, b) => a + (b - m) ** 2, 0) / (xs.length - 1) / xs.length);
};
const fmtTime = (s) => (s >= 3600 ? `${(s / 3600).toFixed(1)}h` : s >= 60 ? `${Math.round(s / 60)}m` : `${Math.round(s)}s`);
const log = (msg) => process.stdout.write(msg + "\n");

// ---------------------------------------------------------------------------- main
async function main() {
  const o = parseArgs(process.argv.slice(2));
  const repo = path.resolve(o.repo);
  const [game, manifestConfig, manifest] = await loadGame(repo);
  if (manifest.runtime && manifest.runtime !== "node") fail("The tuner only supports JavaScript (runtime \"node\") modules for now");

  const base = { ...structuredClone(manifestConfig), ...(o.config ?? {}) };
  const specs = tuneSpecs(o.params.length ? o.params : manifest.tune ?? [], base);
  const referee = structuredClone(base);
  for (const s of specs) deletePath(referee, s.path);
  Object.assign(referee, o.referee ?? {});
  try {
    const probe = game.newGame(1, structuredClone(referee));
    if (typeof probe.withConfig !== "function") fail("The game's state has no withConfig(config) method, which tuning needs");
    probe.withConfig(structuredClone(base));
  } catch (e) {
    fail(`Could not start a game with the referee/base config: ${e.message}`);
  }

  const threads = Math.max(1, Math.floor(o.threads ?? defaultThreads()));
  const n = game.numPlayers;
  let games = Math.max(n, Math.floor(o.games ?? threads));
  games -= games % n; // every config plays every seat equally often
  const checkGames = Math.max(n, Math.floor(o.checkGames ?? games * 2) - (Math.floor(o.checkGames ?? games * 2) % n));
  const rounds = Math.max(1, Math.floor(o.rounds ?? 30));
  const iterations = Math.max(1, Math.floor(o.iterations ?? 150));
  const maxSteps = Number.parseInt(game.maxSteps ?? 500, 10);
  const ctx = {
    numPlayers: n,
    iterations,
    match: { referee, exploration: o.exploration ?? 0.7, rolloutDepth: Number.parseInt(game.rolloutDepth ?? maxSteps, 10), maxSteps },
  };
  const outFile = path.resolve(o.out ?? path.join(repo, "metafinder.tuned.json"));

  // SPSA in log space. c: perturbation size, a: step size, both shrinking over the run.
  const c0 = Math.log(1.2); // try ±20% at first
  const A = Math.max(1, rounds / 10);
  const maxStep = Math.log(1.15); // never move a value more than 15% in one round
  const a0 = 0.04 * c0 / 0.05 * (A + 1) ** 0.602; // a 55/45 result moves values ~4% in the first rounds
  const lo = specs.map((s) => Math.log(s.min));
  const hi = specs.map((s) => Math.log(s.max));
  const clamp = (t) => t.map((x, i) => Math.min(hi[i], Math.max(lo[i], x)));

  let theta = specs.map((s) => Math.log(s.start));
  let startRound = 0;
  let history = [];
  let seed = Math.floor(o.seed ?? Date.now() % 1e9);
  if (o.resume) {
    if (!fs.existsSync(outFile)) fail(`--resume: no checkpoint at ${outFile}`);
    const ck = JSON.parse(fs.readFileSync(outFile, "utf8"));
    const same = JSON.stringify(ck.params?.map((p) => p.path)) === JSON.stringify(specs.map((s) => s.path));
    if (!same) fail("--resume: the checkpoint tuned different values; drop --resume or use the same --param list");
    theta = ck.theta;
    startRound = ck.round;
    history = ck.history ?? [];
    seed = ck.seed;
    for (const [i, s] of specs.entries()) s.start = ck.params[i].start;
  }
  const rand = mulberry32(seed + startRound * 7777);

  const save = (extra = {}) => {
    const tuned = configFor(base, specs, theta);
    const out = {
      note: "Written by MetaFinder's tune.mjs. 'tuned_config' is the result; the rest lets --resume continue.",
      tuned_config: tuned,
      params: specs.map((s, i) => ({ path: s.path, start: s.start, tuned: getPath(tuned, s.path), min: s.min, max: s.max })),
      round: extra.round ?? startRound,
      rounds,
      seed,
      settings: { iterations, games_per_round: games, threads, referee },
      theta,
      history,
      ...extra,
    };
    fs.writeFileSync(outFile, JSON.stringify(out, null, 2) + "\n");
    return tuned;
  };

  log(`Tuning ${game.name}: ${specs.map((s) => s.path).join(", ")}`);
  log(`${rounds - startRound} rounds × ${games} games at ${iterations} iterations/move on ${threads} threads; ` +
    `then ${checkGames} games tuned vs start. Checkpoint: ${outFile}`);

  const pool = new Pool(repo, threads);
  const t0 = performance.now() / 1000;
  try {
    for (let k = startRound; k < rounds; k += 1) {
      const ck = c0 / (k + 1) ** 0.101;
      const ak = a0 / (k + 1 + A) ** 0.602;
      const delta = specs.map(() => (rand() < 0.5 ? -1 : 1));
      const plus = clamp(theta.map((t, i) => t + ck * delta[i]));
      const minus = clamp(theta.map((t, i) => t - ck * delta[i]));
      const res = await duel(pool, ctx, configFor(base, specs, plus), configFor(base, specs, minus), games, seed + k * 1000);
      const score = mean(res.map((r) => r.score));
      const r = score - 1 / n; // > 0: the "+" side was better
      theta = clamp(theta.map((t, i) => t + Math.max(-maxStep, Math.min(maxStep, (ak * r * delta[i]) / ck))));
      history.push({ round: k + 1, plus_score: Math.round(score * 1000) / 1000, theta: theta.map((t) => Math.round(t * 1e4) / 1e4) });
      const tuned = save({ round: k + 1 });

      const elapsed = performance.now() / 1000 - t0;
      const eta = (elapsed / (k + 1 - startRound)) * (rounds - k - 1);
      const values = specs.map((s) => `${s.path}=${getPath(tuned, s.path)}`).join("  ");
      log(`round ${String(k + 1).padStart(3)}/${rounds}  "+" scored ${(score * 100).toFixed(1)}%  ${values}  (${fmtTime(elapsed)}, ~${fmtTime(eta)} left)`);
    }

    // Check: tuned values against the starting values.
    const startTheta = specs.map((s) => Math.log(s.start));
    const tuned = configFor(base, specs, theta);
    const startCfg = configFor(base, specs, startTheta);
    log(`Checking: ${checkGames} games, tuned vs starting values…`);
    const res = await duel(pool, ctx, tuned, startCfg, checkGames, seed + 9_000_000);
    const scores = res.map((r) => r.score);
    const m = mean(scores);
    const ci = 1.96 * stderr(scores);
    const better = m - ci > 1 / n;
    const check = { games: checkGames, score: Math.round(m * 1000) / 1000, ci95: Math.round(ci * 1000) / 1000, better };
    save({ round: rounds, check });

    log("");
    for (const s of specs) log(`  ${s.path.padEnd(28)} ${String(s.start).padStart(10)} → ${getPath(tuned, s.path)}`);
    log(`Tuned vs start: ${(m * 100).toFixed(1)}% ± ${(ci * 100).toFixed(1)}% over ${checkGames} games (50% = no difference).`);
    if (o.write) {
      if (!better) log("Not written to metafinder.json: the tuned values did not beat the start clearly. Try more rounds or games.");
      else {
        const manifestPath = path.join(repo, "metafinder.json");
        const current = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
        current.config ??= {};
        for (const s of specs) setPath(current.config, s.path, getPath(tuned, s.path));
        fs.writeFileSync(manifestPath, JSON.stringify(current, null, 2) + "\n");
        log(`Saved to ${manifestPath}; the next run starts from these values.`);
      }
    } else if (better) {
      log("The tuned values are better. Run again with --write to save them into metafinder.json.");
    }
  } finally {
    await pool.close();
  }
}

await main();
