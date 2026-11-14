// Loads a repository's JavaScript game module (shared by worker.mjs and tune.mjs).

import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

export class GameError extends Error {}

const JS_ENTRY_FILES = ["metafinder_game.mjs", "metafinder_game.js", "metafinder_game.ts"];

/** Imports a repo's game module. Returns [game, manifest config, manifest]. */
export async function loadGame(repoDir) {
  const manifestPath = path.join(repoDir, "metafinder.json");
  let manifest = {};
  let entry;
  if (fs.existsSync(manifestPath)) {
    manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    entry = manifest.entry;
    if (!entry) throw new GameError('metafinder.json must contain an "entry" like "src/adapter.js:GAME"');
  } else {
    const file = JS_ENTRY_FILES.find((f) => fs.existsSync(path.join(repoDir, f)));
    if (!file) throw new GameError("No metafinder.json (or metafinder_game.js) found at the repo root.");
    entry = `${file}:GAME`;
  }

  const m = /^(.*):([A-Za-z_$][\w$]*)$/.exec(entry);
  const file = m ? m[1] : entry;
  const attr = m ? m[2] : "GAME";
  const full = path.resolve(repoDir, file);
  const root = path.resolve(repoDir);
  if (full !== root && !full.startsWith(root + path.sep)) throw new GameError(`Entry '${file}' is outside the repository`);
  if (!fs.existsSync(full)) throw new GameError(`Entry file '${file}' not found (paths are relative to metafinder.json)`);

  let module;
  try {
    module = await import(pathToFileURL(full).href);
  } catch (e) {
    throw new GameError(`Could not import '${file}': ${e?.stack || e}`);
  }
  let game = module[attr];
  if (game === undefined && attr === "GAME") game = module.default; // `export default` also works
  if (game == null) throw new GameError(`'${file}' has no export named '${attr}'`);
  if (typeof game === "function" && typeof game.newGame !== "function") {
    const isClass = /^class[\s{]/.test(Function.prototype.toString.call(game));
    game = isClass ? new game() : game();
  }
  game = await game;

  const missing = ["name", "numPlayers", "perfectInformation", "newGame"].filter((a) => game[a] === undefined);
  if (missing.length) {
    throw new GameError(
      `Game module is missing required properties: ${missing.join(", ")} ` +
      "(JavaScript modules use camelCase: numPlayers, perfectInformation, newGame, legalActions, …)",
    );
  }
  return [game, manifest.config ?? {}, manifest];
}
