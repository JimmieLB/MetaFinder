// Takeaway in JavaScript: the Node.js twin of examples/takeaway.
// Players alternately remove 1..maxTake stones; whoever takes the last stone wins.
// Known optimal strategy: always leave a multiple of (maxTake + 1) stones.

class TakeawayState {
  constructor(stones, maxTake, toMove = 0, winner = null) {
    this.stones = stones;
    this.maxTake = maxTake;
    this.toMove = toMove;
    this.winner = winner;
  }

  // --- required
  clone() {
    return new TakeawayState(this.stones, this.maxTake, this.toMove, this.winner);
  }

  currentPlayer() {
    return this.toMove;
  }

  legalActions() {
    const out = [];
    for (let n = 1; n <= Math.min(this.maxTake, this.stones); n += 1) out.push(n);
    return out;
  }

  apply(n) {
    this.stones -= n;
    if (this.stones === 0) this.winner = this.toMove;
    this.toMove = 1 - this.toMove;
  }

  isTerminal() {
    return this.winner !== null;
  }

  returns() {
    return this.winner === 0 ? [1, 0] : [0, 1];
  }

  // --- optional
  actionLabel(n) {
    return `take ${n}`;
  }

  describe() {
    return `${this.stones} stones left, player ${this.toMove} to move`;
  }

  strategyKey() {
    return `${this.stones} stones left`;
  }

  toJSON() {
    return { stones: this.stones, toMove: this.toMove, winner: this.winner };
  }
}

export const GAME = {
  name: "Takeaway (JS)",
  numPlayers: 2,
  perfectInformation: true,
  maxSteps: 200,
  newGame(seed, config) {
    return new TakeawayState(Number(config.stones ?? 21), Number(config.max_take ?? 3));
  },
};
