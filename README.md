# MetaFinder

Paste a GitHub link to a game that ships a headless MetaFinder module. MetaFinder clones the repo, checks
that the module behaves correctly, then plays the game with Monte Carlo Tree Search and reports:

- the best opening move, with values, proven wins/losses and the expected line of play
- a learned strategy table (the best action per situation, grouped by the game's `strategy_key()`)
- pick rates per action (near 0% means a weak or dominated option)
- seat balance in MCTS-vs-MCTS self-play, plus a skill check against random players
- telemetry from the game's own events, and a commented sample game

## Run

```sh
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m metafinder                         # http://127.0.0.1:8000
```

Three bundled examples appear as buttons on the page:
- `examples/takeaway`: a perfect-information game with a known solution. MetaFinder solves it.
- `examples/takeaway-js`: the same game as a Node.js module.
- `examples/minidecks`: a deckbuilder with hidden hands, random draws and a market.

## Deploy (Railway or any Docker host)

**Read this first:** analysis runs code from the repository that's submitted. On a public URL, anyone who can
reach the page can run programs on your server. The deployment is set up to limit that:

- **Password:** `METAFINDER_PASSWORD` protects the whole site (the browser asks; any user name works). The
  server won't start on a public address without it, unless you also set `METAFINDER_ALLOW_PUBLIC=1`.
- **Separate user:** each analysis runs as an unprivileged `runner` user with a stripped-down environment, so
  it can't read the password or Railway's variables.
- **Resource limits:** each analysis gets CPU-time, memory and file-size limits, and is killed along
  with anything it started once it finishes or times out.
- **No local folders:** they are switched off. GitHub URLs and the bundled examples still work.

Submitted code can still use the network and the CPU until the job limit. Only share the password with people
whose repositories you'd run on your own machine.

**Railway:**

1. Put this folder in a GitHub repository, then in Railway choose **New project → Deploy from GitHub repo**.
   With the Railway CLI, `railway up` from this folder works too.
2. Railway finds `railway.json` and builds the `Dockerfile` (Python 3.12, Node 22, git).
3. Under the service's **Variables**, set `METAFINDER_PASSWORD`. Optionally set `METAFINDER_JOB_TIMEOUT`,
   `METAFINDER_MAX_CONCURRENT` and `METAFINDER_WORKER_MEMORY_MB` (see [example.env](example.env)).
4. Under **Settings → Networking**, generate a domain. Railway supplies `PORT`, and `/api/health` is the
   health check.

Jobs and their results are kept in memory, so they disappear when the service restarts or redeploys. Give
the service enough memory for `METAFINDER_MAX_CONCURRENT` × `METAFINDER_WORKER_MEMORY_MB` plus about 200 MB.

## Making a game compatible

A compatible game ships a **headless rules module**: a small Python or JavaScript (Node.js) object that can start a game, list the
legal moves, apply a move and say who won. It has no graphics, input, sound, network, files or clocks. MetaFinder
loads it straight into the analysis process (Python or Node.js, matching the module) and calls it hundreds of
thousands of times per analysis. Every
MCTS iteration clones the state and plays moves forward, so the rules must be fast and must run in-process.

The typed contract is in [metafinder/protocol.py](metafinder/protocol.py). The working examples in
[examples/takeaway](examples/takeaway) (simple) and [examples/minidecks](examples/minidecks) (chance plus hidden
information) are the best templates.

### Which languages work

| Game written in | Supported? | Notes |
|---|---|---|
| Python 3.10+, standard library only | **Yes** | The intended case. |
| Python with third-party packages (numpy, pygame, …) | **Only if MetaFinder's environment already has them** | MetaFinder does **not** install your `requirements.txt` or `pyproject.toml`. Keep the rules and adapter stdlib-only. Import UI libraries only in modules the adapter never imports. |
| Python game frameworks (pygame, arcade, pyglet, Ren'Py) | **Yes, if the rules are separate from the game loop** | The adapter must not open a window, poll input, `sleep()` or run a frame loop. Move the rules into plain classes and have the UI and the adapter both use them. |
| Python with compiled extensions (Cython, pybind11, Rust/PyO3, …) | **Only with a committed prebuilt binary** | No build step runs. The `.pyd`/`.so` must match the host OS, CPU architecture and Python version. |
| C, C++, Rust, Go, Zig, … as a shared library | **Possible, not recommended** | Write a Python adapter that calls a committed, prebuilt `.dll`/`.so`/`.dylib` through `ctypes`. `clone()` must copy the native state. You still have to ship a binary for each host platform. |
| JavaScript (Node.js 20+), no npm dependencies | **Yes** | Set `"runtime": "node"` in `metafinder.json`. The search runs in Node, in the same process as your rules. See [Node.js modules](#nodejs-modules). |
| TypeScript | **Yes, with Node 22.18+ or committed `.js`** | Node runs `.ts` entries directly when it supports type stripping (erasable syntax only: no `enum` or `namespace`). Otherwise commit the compiled JavaScript. |
| JavaScript with npm packages | **Only if they are committed** | MetaFinder does **not** run `npm install`. Import only Node built-ins and files in the repo. Browser-only code (DOM, `window`, canvas) must stay out of the adapter's imports. |
| Lua, C# (Unity), GDScript (Godot), Java/Kotlin, Unreal, Scratch, … | **No (port the rules)** | MetaFinder cannot load these runtimes. Wrapping them in a subprocess or HTTP call is too slow: an analysis makes millions of `clone`/`apply` calls. Rewrite the rules (not the UI) in Python or JavaScript. This is usually a few hundred lines. |
| Rulebook, spreadsheet, no code | **No** | Write the rules in Python first. |

Also note:
- The repo is cloned with `git clone --depth 1`. **Git submodules and Git LFS files are not fetched**, so the
  rules code cannot live in either.
- Code runs on the Python interpreter MetaFinder runs on (3.12 in the Docker image; check yours when self-hosting), so
  target Python 3.10 syntax to be safe.
- Anything the module prints goes to the worker log. Printing is harmless, but it slows the search.

### Repository layout

```
my-game/                          ← repo root, or the sub-folder you link to with /tree/<branch>/<path>
├── metafinder.json               ← manifest: tells MetaFinder where the game object is
├── my_game/
│   ├── __init__.py
│   ├── rules.py                  ← pure game logic (no UI imports)
│   └── metafinder_adapter.py     ← defines GAME, wraps rules.py in the contract below
└── ui/, assets/, …               ← ignored by MetaFinder (just never imported by the adapter)
```

`metafinder.json`:

```json
{ "entry": "my_game.metafinder_adapter:GAME", "config": { "starting_hp": 20 } }
```

| Field | Required | Meaning |
|---|---|---|
| `entry` | yes | Python: `module.path:attribute`. Node: `path/to/file.js:EXPORT`. Paths are relative to the folder holding `metafinder.json`. The attribute or export defaults to `GAME`. |
| `runtime` | no | `"python"` (default) or `"node"`. If omitted, an entry ending in `.js`, `.mjs`, `.cjs`, `.ts` or `.mts` means Node. |
| `config` | no | A dict passed to `new_game(seed, config)`. Values typed into the UI's config box override these keys. |

Loading works like this: the folder is added to `sys.path`, the module is imported, and the attribute is read. If
the attribute is callable and has no `new_game` (a class or factory, for example), it is called with no arguments.
**Shortcut:** with no `metafinder.json`, a `metafinder_game.py` defining `GAME` in the folder is used instead.

Naming pitfalls: don't name a top-level module or package `metafinder` (MetaFinder's own package wins). Don't
shadow standard-library names such as `random.py`, `json.py`, `types.py` or `copy.py` at the folder root.

### The game object (`GAME`)

| Attribute | Required | Meaning |
|---|---|---|
| `name: str` | yes | Display name. |
| `num_players: int` | yes | 1 or more. Players are numbered `0..num_players-1`. |
| `perfect_information: bool` | yes | `False` if any player ever lacks information another player or the game has (hands, decks, face-down cards, hidden choices). |
| `new_game(seed, config) -> State` | yes | Fresh state. Use `seed` for any setup randomness and **only** `seed`. |
| `max_steps: int` | no (500) | Hard cap on actions per game. A game that hits it counts as a timeout. |
| `rollout_depth: int` | no (`max_steps`) | Cuts random playouts after this many steps and scores them with `heuristic_value()`. |
| `default_rollout_policy(state, rng) -> action` | no | A smarter-than-random move picker for playouts. It is called only on player turns and must return a legal action. |

### The state object

| Method | Required | Contract |
|---|---|---|
| `clone()` | yes | Deep, fully independent copy. **It must be fast**: copy lists and dicts by hand rather than with `copy.deepcopy`. |
| `current_player()` | yes | `0..n-1`, or `-1` when chance acts (a shuffle, draw or dice roll). |
| `legal_actions()` | yes | A non-empty list for every non-terminal player turn (add a "pass" action if needed), with no duplicates and a deterministic order. |
| `apply(action)` | yes | Mutates the state in place. It must be **deterministic**: the same state plus the same action always gives the same result. |
| `is_terminal()` | yes | `True` once the game is over. |
| `returns()` | yes | Called on terminal states. Gives one float per player in `[0, 1]`: 1 = win, 0 = loss, 0.5 each for a two-player draw. Normalize score-based games, for example by rank or by `score / max_score`. |
| `chance_outcomes()` | if `-1` ever occurs | A list of `(outcome, probability)` pairs whose probabilities sum to 1. Outcomes follow the same rules as actions. Alternative: `sample_chance(rng) -> outcome`. |
| `determinize(observer, rng)` | if `perfect_information = False` | Returns a **new** state in which everything `observer` can't see is re-randomized, consistently with what they can see. The observer's own hand, `current_player()` and legal actions must not change. The original must not be mutated. |
| `heuristic_value()` | recommended for long games | Same shape as `returns()`: an estimate for a game that isn't finished. Used when a playout reaches `rollout_depth` or a game reaches `max_steps`. |
| `to_json()` | strongly recommended | The **complete** state as JSON-serializable data. The conformance checks compare snapshots to catch broken `clone()`, `determinize()` and replays. Without it those checks are much weaker. |
| `describe()` | no | A one-line, human-readable position for the report and the sample game. |
| `action_label(action)` | no | Display text for an action (defaults to `str(action)`). |
| `strategy_key()` | no | A string that buckets similar decisions (for example "5 coins, buy phase"). It drives the learned strategy table. |
| `drain_events()` | no | Returns **and clears** a list of `{"type": ..., ...}` dicts since the last call, for telemetry. Keys `player`, `turn`, `tick`, `t` and `step`, and nested values, are ignored when grouping events. |

### Rules for actions

- **Hashable and compared by value**: `int`, `str`, tuples, frozen dataclasses, or `NamedTuple`. Equal moves in
  two different clones must compare equal and hash the same.
- **Not allowed**: lists, dicts, or object instances that use default identity equality. A `Card` object passed
  as an action breaks the search, because the clone's `Card` won't equal the original's.
- **Unique labels**: the reports aggregate by `action_label()` / `str()`, so two different moves should never print
  the same text.
- **Keep the count small**: MCTS works best with tens of options per decision and struggles above a few hundred.
  Abstract big action spaces (for example "attack the weakest unit" instead of every coordinate).

### Randomness and hidden information (the most common bugs)

1. **No hidden randomness.** Inside `apply()`, `clone()` or `legal_actions()`, never use the global `random`
   module, `time`, `uuid`, `os.urandom` or anything else that changes between runs. The conformance checks replay
   every game from its seed and fail if the result differs.
2. **Random events after setup are chance nodes.** Set `current_player()` to `-1` and list the outcomes in
   `chance_outcomes()`. Pre-shuffling a deck in `new_game` is allowed. But in a perfect-information game the
   search then knows every future draw, so either model draws as chance nodes or declare hidden information.
3. **Don't build `legal_actions()` from a `set` of strings.** The order would change between runs (hash
   randomization). Use lists or `sorted()`.
4. **`determinize()` must not leak or lose information.** Shuffle only what the observer can't see (the
   opponent's hand together with the unseen deck, for example), and keep public facts such as counts and
   revealed cards fixed.

### Modelling awkward games

| Game feature | How to model it |
|---|---|
| Multi-part turns (play cards, then buy, then end) | One action per decision, with an explicit "end turn" action. See `minidecks`. |
| Simultaneous moves (rock-paper-scissors, sealed bids) | Make them sequential: player 0 commits, then player 1 chooses without seeing it. Set `perfect_information = False` and have `determinize()` re-randomize the pending choice. |
| Real-time or continuous games | Break play into discrete decision points with a finite set of actions. |
| Single-player and puzzles | `num_players = 1`. `returns()` is `[score in 0..1]`. |
| 3 or more players | Supported as is. Return one value per player. |
| Very long games or games that can loop | Set `rollout_depth`, implement `heuristic_value()`, and make sure a turn limit ends the game. |

### Testing locally

While `METAFINDER_ALLOW_LOCAL=1` (the default), paste a local folder path into the URL box instead of a GitHub
link. The **Conformance** phase runs six random games and reports problems such as broken clones, replay
mismatches, bad `returns()` or a leaky `determinize()` before any analysis starts. Errors stop the run, while
warnings let it continue.

### Minimal adapter

```python
# my_game/metafinder_adapter.py
from dataclasses import dataclass

@dataclass(frozen=True)
class Move:            # hashable and compared by value
    n: int
    def __str__(self): return f"take {self.n}"

class State:
    def __init__(self, stones, to_move=0, winner=None):
        self.stones, self.to_move, self.winner = stones, to_move, winner
    def clone(self):            return State(self.stones, self.to_move, self.winner)
    def current_player(self):   return self.to_move
    def legal_actions(self):    return [Move(n) for n in range(1, min(3, self.stones) + 1)]
    def apply(self, a):
        self.stones -= a.n
        if self.stones == 0: self.winner = self.to_move
        self.to_move = 1 - self.to_move
    def is_terminal(self):      return self.winner is not None
    def returns(self):          return [1.0, 0.0] if self.winner == 0 else [0.0, 1.0]
    def to_json(self):          return {"stones": self.stones, "to_move": self.to_move, "winner": self.winner}

class Game:
    name = "Takeaway"
    num_players = 2
    perfect_information = True
    def new_game(self, seed, config):
        return State(int(config.get("stones", 21)))

GAME = Game()
```

## Node.js modules

A JavaScript module follows the same contract as above, with camelCase names. MetaFinder runs a JavaScript port
of the search ([metafinder/node/](metafinder/node)) in Node, importing your module into that process, so there is
no per-call overhead. The host needs Node 20+ on `PATH` (or `METAFINDER_NODE` set to its path).

```json
{ "runtime": "node", "entry": "src/metafinder.js:GAME", "config": { "stones": 21 } }
```

| Python | JavaScript |
|---|---|
| `num_players`, `perfect_information`, `max_steps`, `rollout_depth` | `numPlayers`, `perfectInformation`, `maxSteps`, `rolloutDepth` |
| `new_game(seed, config)`, `default_rollout_policy(state, rng)` | `newGame(seed, config)`, `defaultRolloutPolicy(state, rng)` |
| `current_player()`, `legal_actions()`, `is_terminal()` | `currentPlayer()`, `legalActions()`, `isTerminal()` |
| `chance_outcomes()` → `(outcome, p)` pairs, `sample_chance(rng)` | `chanceOutcomes()` → `[outcome, p]` arrays, `sampleChance(rng)` |
| `heuristic_value()`, `drain_events()`, `to_json()` | `heuristicValue()`, `drainEvents()`, `toJSON()` |
| `action_label(a)`, `strategy_key()` | `actionLabel(a)`, `strategyKey()` |
| `clone()`, `apply(a)`, `returns()`, `determinize(observer, rng)`, `describe()` | unchanged |

JavaScript-specific rules:

- **Actions are compared by value through a key**: strings and numbers are used as-is, while arrays and objects
  are keyed with `JSON.stringify`. Strings such as `"move:3:4"` are fastest. Objects must build their keys in a
  fixed order, and the same move must never appear both as the number `1` and the string `"1"`.
- **The module must be ESM** (`export const GAME = …`; `export default` also works). Use `.mjs`, or `.js` in a
  package whose `package.json` has `"type": "module"`. A class or a factory function export is instantiated or
  called with no arguments.
- **`rng`** is MetaFinder's seeded generator with `random()` (a float in `[0, 1)`), `int(n)` and `choice(list)`.
  Inside the rules, use it, `seed` or chance nodes, never `Math.random()` or `Date.now()`.
- `console.log` output goes to the worker log.
- **Without `metafinder.json`**, a `metafinder_game.mjs`, `.js` or `.ts` exporting `GAME` is used instead.

See [examples/takeaway-js](examples/takeaway-js) for a complete module.

## How it works

- `metafinder/mcts.py`: single-observer IS-MCTS with chance nodes. Perfect-information games also get an
  MCTS-Solver, which propagates proven wins and losses up the tree.
- `metafinder/worker.py`: runs as a subprocess. Phases: load → conformance checks → opening search →
  self-play → baseline against random players. It streams JSON-lines progress.
- `metafinder/node/mcts.mjs`, `metafinder/node/worker.mjs`: the same search and worker in JavaScript, used
  for `"runtime": "node"` modules. They produce identical result JSON, so the UI doesn't need to know which ran.
- `metafinder/server.py`: FastAPI. Clones with `git clone --depth 1`, runs one worker per job (default
  limit is 2 at a time, with a timeout), and serves `static/index.html`.

Settings are environment variables: `METAFINDER_JOB_TIMEOUT` (seconds, default 900), `METAFINDER_MAX_CONCURRENT`
(default 2), `METAFINDER_ALLOW_LOCAL` (default `1`: accepts local paths and shows the example buttons),
`METAFINDER_JOBS_DIR` and `METAFINDER_NODE`. To keep them in a file, copy [example.env](example.env) to `.env`.
`python -m metafinder` reads it at startup, or another file passed with `--env-file`. Variables already set in the
environment take priority.

## ⚠️ Security

Analysis **runs the submitted repository's Python code**. The subprocess and timeout guard against hangs, but
they are not a sandbox. Keep the server on localhost. Before exposing it, run the worker inside a container
(no network, read-only filesystem, CPU and memory limits) and set `METAFINDER_ALLOW_LOCAL=0`.
