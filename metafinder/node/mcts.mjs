// Single-observer Information-Set MCTS with chance nodes: a port of metafinder/mcts.py for games
// written in JavaScript. Same algorithm, same tie-breaking rules, different RNG stream.
//
// Actions can be any JSON-serializable value. Strings and numbers are cheapest: they are used
// as map keys almost directly, while objects/arrays go through JSON.stringify.

export const CHANCE = -1;

/** Map key for an action. Strings and other values never collide ("s1" vs "1"). */
export const actionKey = (a) => (typeof a === "string" ? "s" + a : JSON.stringify(a));

/** Small, fast, seedable PRNG (sfc32). random() returns a float in [0, 1). */
export class Rng {
  constructor(seed = Date.now()) {
    let s = (Number(seed) >>> 0) ^ 0x9e3779b9;
    this.a = 0x243f6a88 ^ s;
    this.b = 0x85a308d3 + s;
    this.c = 0x13198a2e ^ (s * 31);
    this.d = 1;
    for (let i = 0; i < 15; i += 1) this.random();
  }

  random() {
    let { a, b, c, d } = this;
    const t = (((a + b) | 0) + d) | 0;
    d = (d + 1) | 0;
    a = b ^ (b >>> 9);
    b = (c + (c << 3)) | 0;
    c = (c << 21) | (c >>> 11);
    c = (c + t) | 0;
    this.a = a; this.b = b; this.c = c; this.d = d;
    return (t >>> 0) / 4294967296;
  }

  /** Integer in [0, n). */
  int(n) {
    return Math.floor(this.random() * n);
  }

  choice(list) {
    return list[Math.floor(this.random() * list.length)];
  }
}

export class Node {
  constructor(parent, action, player, nPlayers) {
    this.parent = parent;
    this.action = action;
    this.player = player; // who took `action` to reach this node (CHANCE for outcomes)
    this.children = new Map(); // actionKey -> Node
    this.visits = 0;
    this.valueSum = new Array(nPlayers).fill(0);
    this.avail = 1;
    this.toMove = null; // who acts at this node's state (perfect info only)
    this.nLegal = 0;
    this.proven = null; // exact game value, once solved
  }

  mean(player) {
    if (this.proven !== null) return this.proven[player];
    return this.visits ? this.valueSum[player] / this.visits : 0;
  }
}

export function sampleChance(state, rng) {
  if (typeof state.chanceOutcomes === "function") {
    const pairs = state.chanceOutcomes();
    let total = 0;
    for (const [, p] of pairs) total += p;
    const r = rng.random() * total;
    let acc = 0;
    for (const [action, p] of pairs) {
      acc += p;
      if (r <= acc) return action;
    }
    return pairs[pairs.length - 1][0];
  }
  return state.sampleChance(rng);
}

export function terminalOrHeuristic(state, nPlayers) {
  if (state.isTerminal()) return Array.from(state.returns());
  if (typeof state.heuristicValue === "function") return Array.from(state.heuristicValue());
  return new Array(nPlayers).fill(1 / nPlayers);
}

export class MCTS {
  constructor({
    nPlayers, perfectInformation, iterations = 1000, timeLimit = null, exploration = 0.7,
    maxDepth = 200, rolloutPolicy = null, rng = new Rng(), reuseTree = false,
  }) {
    this.nPlayers = nPlayers;
    this.perfectInformation = perfectInformation;
    this.iterations = iterations;
    this.timeLimit = timeLimit;
    this.c = exploration;
    this.maxDepth = maxDepth;
    this.rolloutPolicy = rolloutPolicy;
    this.rng = rng;
    this.solver = perfectInformation;
    // Tree reuse: keep the subtree under the moves actually played, so the next search starts with
    // the visits it already has. Only sound with perfect information (the tree is keyed by the
    // real sequence of actions); the caller must report every action with advance().
    this.reuseTree = Boolean(reuseTree && perfectInformation);
    this.tree = null;
  }

  /** Reports an action played in the real game (by any player, or chance) for tree reuse. */
  advance(action) {
    if (!this.tree) return;
    const child = this.tree.children.get(actionKey(action));
    if (child) child.parent = null; // lets the rest of the old tree be garbage-collected
    this.tree = child ?? null;
  }

  /** Forgets the kept tree (e.g. before a new game). */
  resetTree() {
    this.tree = null;
  }

  /** The kept subtree if it matches `state`, else null. */
  reusableRoot(state) {
    const root = this.tree;
    this.tree = null;
    if (!root) return null;
    const player = state.currentPlayer();
    if (root.toMove !== null && root.toMove !== player) return null;
    if (player !== CHANCE) {
      const legal = new Set(state.legalActions().map(actionKey));
      for (const key of root.children.keys()) if (!legal.has(key)) return null; // out of sync: start over
    }
    return root;
  }

  search(rootState, observer, onProgress = null) {
    const root = (this.reuseTree && this.reusableRoot(rootState)) || new Node(null, null, observer, this.nPlayers);
    const deadline = this.timeLimit ? performance.now() + this.timeLimit * 1000 : null;
    const reportEvery = Math.max(1, Math.floor(this.iterations / 20));
    for (let i = 0; i < this.iterations; i += 1) {
      if (deadline && i % 32 === 0 && performance.now() > deadline) break;
      if (onProgress && i % reportEvery === 0) onProgress(i);
      const state = this.perfectInformation ? rootState.clone() : rootState.determinize(observer, this.rng);
      let [node, depth] = this.selectExpand(root, state);
      const values = node.proven !== null ? node.proven : this.rollout(state, depth);
      while (node !== null) {
        node.visits += 1;
        for (let p = 0; p < this.nPlayers; p += 1) node.valueSum[p] += values[p];
        if (this.solver) this.tryProve(node);
        node = node.parent;
      }
      if (root.proven !== null) break; // game-theoretic value of the root is known
    }
    if (this.reuseTree) this.tree = root;
    return root;
  }

  selectExpand(node, state) {
    let depth = 0;
    const { rng } = this;
    while (depth < this.maxDepth && !state.isTerminal()) {
      if (node.proven !== null) return [node, depth];
      const player = state.currentPlayer();
      if (player === CHANCE) {
        const outcome = sampleChance(state, rng);
        const key = actionKey(outcome);
        let child = node.children.get(key);
        if (!child) {
          child = new Node(node, outcome, CHANCE, this.nPlayers);
          node.children.set(key, child);
        }
        state.apply(outcome);
        node = child;
        depth += 1;
        continue;
      }

      const legal = state.legalActions();
      if (this.solver) {
        node.toMove = player;
        node.nLegal = legal.length;
        const win = provenWin(node, player);
        if (win) {
          state.apply(win.action);
          node = win;
          depth += 1;
          continue;
        }
      }
      const keys = legal.map(actionKey);
      const untried = [];
      for (let i = 0; i < legal.length; i += 1) {
        const child = node.children.get(keys[i]);
        if (child) child.avail += 1;
        else untried.push(i);
      }
      if (untried.length) {
        const i = rng.choice(untried);
        const child = new Node(node, legal[i], player, this.nPlayers);
        node.children.set(keys[i], child);
        state.apply(legal[i]);
        if (this.solver && state.isTerminal()) child.proven = Array.from(state.returns());
        return [child, depth + 1];
      }

      let best = null;
      let bestScore = -Infinity;
      for (const key of keys) {
        const child = node.children.get(key);
        if (child.proven !== null && child.proven[player] <= 1e-9 && best !== null) continue; // proven loss
        const score = child.mean(player) + this.c * Math.sqrt(Math.log(child.avail) / child.visits);
        if (score > bestScore) {
          best = child;
          bestScore = score;
        }
      }
      state.apply(best.action);
      node = best;
      depth += 1;
    }
    if (this.solver && state.isTerminal()) node.proven = Array.from(state.returns());
    return [node, depth];
  }

  tryProve(node) {
    const p = node.toMove;
    if (node.proven !== null || p === null || p === CHANCE || node.children.size === 0) return;
    const win = provenWin(node, p);
    if (win) {
      node.proven = win.proven;
      return;
    }
    if (node.children.size !== node.nLegal) return;
    let best = null;
    for (const c of node.children.values()) {
      if (c.proven === null) return;
      if (best === null || c.proven[p] > best[p]) best = c.proven;
    }
    node.proven = best;
  }

  rollout(state, depth) {
    const { rng, rolloutPolicy: policy } = this;
    while (depth < this.maxDepth && !state.isTerminal()) {
      if (state.currentPlayer() === CHANCE) state.apply(sampleChance(state, rng));
      else if (policy) state.apply(policy(state, rng));
      else state.apply(rng.choice(state.legalActions()));
      depth += 1;
    }
    return terminalOrHeuristic(state, this.nPlayers);
  }

  /** Returns [action, root]. */
  bestAction(state, player) {
    const root = this.search(state, player);
    const legal = state.legalActions();
    const candidates = [];
    for (const a of legal) {
      const c = root.children.get(actionKey(a));
      if (c) candidates.push(c);
    }
    if (!candidates.length) return [this.rng.choice(legal), root]; // never return an illegal move
    return [rankChildren(candidates, player)[0].action, root];
  }
}

function provenWin(node, player) {
  for (const child of node.children.values()) {
    if (child.proven !== null && child.proven[player] >= 1 - 1e-9) return child;
  }
  return null;
}

export function proofStatus(node, player) {
  if (node.proven === null) return null;
  const v = node.proven[player];
  return v >= 1 - 1e-9 ? "win" : v <= 1e-9 ? "loss" : "draw";
}

/** Proven wins first, proven losses last, everything else by visit count. */
export function rankChildren(children, player) {
  const order = (c) => ({ win: 0, loss: 2 })[proofStatus(c, player)] ?? 1;
  return [...children].sort((a, b) => order(a) - order(b) || b.visits - a.visits || b.mean(player) - a.mean(player));
}
