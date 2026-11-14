"""MetaFinder web server: accepts a GitHub URL, clones it, runs the MCTS analysis worker.

Security note: analysis executes code from the submitted repository. The worker
runs in a separate process with a timeout, but that is NOT a sandbox. Only run
this server locally or put the worker inside a container/VM before exposing it.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
EXAMPLES = ROOT / "examples"
JOBS_DIR = Path(os.environ.get("METAFINDER_JOBS_DIR", ROOT / ".jobs"))
JOB_TIMEOUT = int(os.environ.get("METAFINDER_JOB_TIMEOUT", 900))
MAX_CONCURRENT = int(os.environ.get("METAFINDER_MAX_CONCURRENT", 2))
ALLOW_LOCAL = os.environ.get("METAFINDER_ALLOW_LOCAL", "1") == "1"
# HTTP Basic auth for the whole site (any user name) when set. Required for public hosting.
PASSWORD = os.environ.get("METAFINDER_PASSWORD", "")
# Linux only, when the server runs as root (as in the Docker image): run workers as this user instead,
# so submitted code can't read the server's environment or files it doesn't own.
WORKER_USER = os.environ.get("METAFINDER_WORKER_USER", "")
WORKER_MEMORY_MB = int(os.environ.get("METAFINDER_WORKER_MEMORY_MB", 1536))
POSIX = os.name == "posix"
# AI training (tune.mjs) runs games on several threads and gets its own, usually longer, time limit.
# A run that reaches it stops cleanly and its progress can be resumed later.
TUNE_TIMEOUT = int(os.environ.get("METAFINDER_TUNE_TIMEOUT", JOB_TIMEOUT))
TUNE_THREADS = int(os.environ.get("METAFINDER_TUNE_THREADS", 0))  # 0 = CPU cores − 1
CHECKPOINT = "metafinder.tuned.json"

GITHUB_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:/tree/(?P<ref>[A-Za-z0-9_.\-]+)(?:/(?P<sub>[A-Za-z0-9_.\-/]+?))?)?/?$"
)

app = FastAPI(title="MetaFinder")


@app.middleware("http")
async def require_password(request: Request, call_next):
    """HTTP Basic auth when METAFINDER_PASSWORD is set (the browser shows its own login prompt)."""
    if not PASSWORD or request.url.path == "/api/health":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            _user, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            given = ""
        if secrets.compare_digest(given.encode(), PASSWORD.encode()):
            return await call_next(request)
    return Response("Password required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="MetaFinder"'})


_slots = threading.Semaphore(MAX_CONCURRENT)
_jobs: dict[str, dict] = {}
_procs: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()


class AnalyzeRequest(BaseModel):
    repo_url: str = Field(..., max_length=500)
    iterations: int = Field(800, ge=20, le=50_000, description="MCTS iterations per move in self-play")
    games: int = Field(20, ge=2, le=500)
    opening_iterations: int = Field(20_000, ge=100, le=1_000_000)
    exploration: float = Field(0.7, gt=0, le=5)
    rollout_depth: int | None = Field(None, ge=1, le=100_000)
    seed: int = 1
    config: dict = Field(default_factory=dict)


class TuneRequest(BaseModel):
    repo_url: str = Field(..., max_length=500)
    rounds: int = Field(20, ge=1, le=2000, description="Training rounds in this run (on top of any saved progress)")
    games: int | None = Field(None, ge=2, le=1000, description="Games per round (default: one per thread)")
    iterations: int = Field(150, ge=10, le=50_000, description="MCTS iterations per move while training")
    exploration: float = Field(0.7, gt=0, le=5)
    config: dict = Field(default_factory=dict)
    checkpoint: dict | None = Field(None, description="Saved training progress (a metafinder.tuned.json) to resume")


TUNE_MB_PER_THREAD = 256  # each training thread loads its own copy of the game


def _cgroup_cpus() -> int | None:
    """CPUs allowed by the container's CPU quota, or None if unlimited. Containers (Railway, docker --cpus)
    usually see every core of the host but may only use a few, and os.cpu_count() doesn't know that."""
    def read(p: str) -> str | None:
        try:
            return Path(p).read_text().strip()
        except OSError:
            return None
    v2 = read("/sys/fs/cgroup/cpu.max")  # "max 100000" or "<quota> <period>"
    if v2:
        quota, _, period = v2.partition(" ")
        if quota != "max" and quota.isdigit() and period.isdigit() and int(period) > 0:
            return max(1, -(-int(quota) // int(period)))
        return None
    quota, period = read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"), read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota and period and quota.isdigit() and period.isdigit() and int(quota) > 0 and int(period) > 0:
        return max(1, -(-int(quota) // int(period)))
    return None


def available_cpus() -> int:
    try:
        cpus = len(os.sched_getaffinity(0))
    except AttributeError:
        cpus = os.cpu_count() or 2
    quota = _cgroup_cpus()
    return min(cpus, quota) if quota else cpus


def tune_threads() -> int:
    """Threads per training run: METAFINDER_TUNE_THREADS, else CPUs − 1, limited by the memory budget and
    shared between the runs allowed at once."""
    if TUNE_THREADS > 0:
        return TUNE_THREADS
    by_cpu = max(1, (available_cpus() - 1) // max(1, MAX_CONCURRENT))
    by_memory = max(1, WORKER_MEMORY_MB // TUNE_MB_PER_THREAD)
    return min(by_cpu, by_memory)


def parse_source(url: str) -> dict:
    url = url.strip()
    m = GITHUB_RE.match(url)
    if m:
        sub = m.group("sub") or ""
        if ".." in sub.split("/"):
            raise HTTPException(400, "Invalid sub-directory in URL")
        return {"kind": "github", "clone_url": f"https://github.com/{m.group('owner')}/{m.group('repo')}.git",
                "ref": m.group("ref"), "subdir": sub, "display": url}
    local = (ROOT / url).resolve() if not os.path.isabs(url) else Path(url).resolve()
    bundled = EXAMPLES.resolve() in local.parents  # the bundled examples work even with local paths off
    if (ALLOW_LOCAL or bundled) and local.is_dir():
        return {"kind": "local", "path": str(local), "display": url}
    raise HTTPException(400, "Enter a GitHub repository URL like https://github.com/owner/repo "
                             "(optionally /tree/branch/sub/dir)")


def _update(job_id: str, **fields) -> None:
    with _lock:
        _jobs[job_id].update(fields)


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):  # git pack files are read-only on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerror)


JS_EXTENSIONS = (".js", ".mjs", ".cjs", ".ts", ".mts")
NODE_ENTRY_FILES = ("metafinder_game.mjs", "metafinder_game.js", "metafinder_game.ts")


def detect_runtime(repo_dir: Path) -> str:
    """'python' or 'node'. Uses metafinder.json's "runtime", else the entry file's extension."""
    manifest = repo_dir / "metafinder.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise RuntimeError(f"metafinder.json is not valid JSON: {e}") from e
        runtime = str(data.get("runtime", "")).strip().lower()
        if runtime in ("node", "nodejs", "javascript", "js"):
            return "node"
        if runtime in ("python", "py"):
            return "python"
        if runtime:
            raise RuntimeError(f'Unknown "runtime" {runtime!r} in metafinder.json (use "python" or "node")')
        entry_file = str(data.get("entry", "")).rsplit(":", 1)[0]
        return "node" if entry_file.lower().endswith(JS_EXTENSIONS) else "python"
    if not (repo_dir / "metafinder_game.py").is_file() and any((repo_dir / f).is_file() for f in NODE_ENTRY_FILES):
        return "node"
    return "python"


def node_binary() -> str:
    node = os.environ.get("METAFINDER_NODE") or shutil.which("node")
    if not node:
        raise RuntimeError("This game is a Node.js module, but Node.js isn't installed on the MetaFinder host "
                           "(install Node 20+ or set METAFINDER_NODE to its path)")
    return node


def worker_command(runtime: str, repo_dir: Path, params_path: Path) -> list[str]:
    if runtime == "node":
        cmd = [node_binary(), f"--max-old-space-size={WORKER_MEMORY_MB}",
               str(ROOT / "metafinder" / "node" / "worker.mjs"), "--repo", str(repo_dir), "--params", str(params_path)]
    else:
        cmd = [sys.executable, "-m", "metafinder.worker", "--repo", str(repo_dir), "--params", str(params_path)]
    return limited(cmd, runtime, JOB_TIMEOUT + 60)


def tune_command(repo_dir: Path, out_path: Path, params: dict, resume: bool) -> list[str]:
    threads = tune_threads()
    # The main thread only coordinates; the game threads share the memory budget (--memory-mb caps each heap).
    cmd = [node_binary(), "--max-old-space-size=256", str(ROOT / "metafinder" / "node" / "tune.mjs"),
           "--repo", str(repo_dir), "--out", str(out_path), "--jsonl", "--threads", str(threads),
           "--memory-mb", str(WORKER_MEMORY_MB),
           "--iterations", str(params["iterations"]), "--exploration", str(params["exploration"]),
           # Stop early enough to finish cleanly (and save) before the hard kill.
           "--time-limit", str(max(30, TUNE_TIMEOUT - 30))]
    cmd += ["--resume", "--more-rounds", str(params["rounds"])] if resume else ["--rounds", str(params["rounds"])]
    if params.get("games"):
        cmd += ["--games", str(params["games"])]
    if params.get("config"):
        cmd += ["--config", json.dumps(params["config"])]
    # CPU time is counted across all threads.
    return limited(cmd, "node", (TUNE_TIMEOUT + 60) * (threads + 1))


def limited(cmd: list[str], runtime: str, cpu_seconds: int) -> list[str]:
    bash = shutil.which("bash") if POSIX else None
    if bash:
        # Resource limits for the submitted code: CPU seconds, file size (KB) and (Python only; V8 reserves
        # far more address space than it uses) memory. No process limit (ulimit -u): Linux counts it per
        # user ID across the whole host, so on shared container hosts it's already used up by other
        # containers and Node can't even start its threads. The container's own pids limit covers that.
        limits = f"ulimit -t {cpu_seconds} && ulimit -f 262144"
        if runtime != "node":
            limits += f" && ulimit -v {WORKER_MEMORY_MB * 1024}"
        cmd = [bash, "-c", f'{limits} && exec "$@"', "metafinder-worker", *cmd]
    return cmd


def worker_env() -> dict[str, str]:
    """A minimal environment: the server's own variables (secrets, tokens) are not passed on."""
    keep = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1",
               HOME=os.environ.get("TEMP", "/tmp") if not POSIX else "/tmp")
    return env


def worker_user_kwargs() -> dict:
    if POSIX and WORKER_USER and os.geteuid() == 0:
        return {"user": WORKER_USER, "group": WORKER_USER, "extra_groups": []}
    return {}


def _kill(proc: subprocess.Popen) -> None:
    """Kills the worker and anything it started (it runs in its own process group on Linux)."""
    try:
        if POSIX:
            os.killpg(proc.pid, 9)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _fetch(job_id: str, source: dict, job_dir: Path) -> Path:
    """Clones a GitHub source into job_dir/repo (or returns the local folder). Returns the game folder."""
    if source["kind"] != "github":
        return Path(source["path"])
    _update(job_id, status="cloning", phase="clone", message=f"Cloning {source['clone_url']}")
    dest = job_dir / "repo"
    cmd = ["git", "clone", "--depth", "1", "--single-branch", "--no-tags"]
    if source["ref"]:
        cmd += ["--branch", source["ref"]]
    cmd += [source["clone_url"], str(dest)]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    if r.returncode != 0:
        raise RuntimeError("git clone failed: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1])
    repo_dir = (dest / source["subdir"]).resolve()
    if dest.resolve() not in [repo_dir, *repo_dir.parents] or not repo_dir.is_dir():
        raise RuntimeError(f"Sub-directory '{source['subdir']}' not found in the repository")
    return repo_dir


def _stream(job_id: str, cmd: list[str], job_dir: Path, timeout: int, done_message: str) -> None:
    """Runs a worker that speaks the JSON-lines protocol and mirrors its progress into the job."""
    with open(job_dir / "worker.log", "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=str(job_dir), env=worker_env(), stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8",
            start_new_session=POSIX, **worker_user_kwargs(),
        )
        _procs[job_id] = proc
        timed_out = threading.Event()
        killer = threading.Timer(timeout, lambda: (timed_out.set(), _kill(proc)))
        killer.start()
        got_result = False
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "progress":
                    _update(job_id, phase=msg.get("phase"), pct=msg.get("pct", 0), message=msg.get("message", ""))
                elif msg.get("type") == "result":
                    got_result = True
                    _update(job_id, status="done", phase="done", pct=1, message=done_message,
                            result=msg, finished=time.time())
                elif msg.get("type") == "error":
                    got_result = True
                    _update(job_id, status="error", message=msg.get("message", "Error"),
                            traceback=msg.get("traceback"), finished=time.time())
            proc.wait()
        finally:
            killer.cancel()
            if POSIX:
                _kill(proc)  # anything the game code left running in the worker's process group
            _procs.pop(job_id, None)

    if _jobs[job_id]["status"] == "cancelled" or got_result:
        return
    tail = (job_dir / "worker.log").read_text(encoding="utf-8", errors="replace")[-2000:]
    reason = f"timed out after {timeout}s; try a smaller budget" if timed_out.is_set() \
        else f"exited with code {proc.returncode}"
    _update(job_id, status="error", message=f"Worker {reason}", traceback=tail, finished=time.time())


def _run_job(job_id: str, source: dict, params: dict, kind: str = "analyze", checkpoint: dict | None = None) -> None:
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    _update(job_id, status="queued", message="Waiting for a free worker slot")
    with _slots:
        if _jobs[job_id]["status"] == "cancelled":
            return
        try:
            repo_dir = _fetch(job_id, source, job_dir)
            runtime = detect_runtime(repo_dir)
            if kind == "tune":
                _run_tune(job_id, job_dir, repo_dir, runtime, params, checkpoint)
            else:
                params_path = job_dir / "params.json"
                # The worker paces itself to finish (with partial results if need be) before the hard kill.
                worker_params = {**params, "time_budget": max(30, JOB_TIMEOUT - 20)}
                params_path.write_text(json.dumps(worker_params), encoding="utf-8")
                cmd = worker_command(runtime, repo_dir, params_path)
                _update(job_id, status="running", phase="load", runtime=runtime,
                        message=f"Starting analysis worker ({'Node.js' if runtime == 'node' else 'Python'})")
                _stream(job_id, cmd, job_dir, JOB_TIMEOUT, "Analysis complete")
        except subprocess.TimeoutExpired:
            _update(job_id, status="error", message="git clone timed out", finished=time.time())
        except Exception as e:
            _update(job_id, status="error", message=str(e), finished=time.time())
        finally:
            if kind == "tune":
                try:
                    _collect_training(job_id, job_dir)
                except Exception as e:  # never lose the job's own status over the downloads
                    _update(job_id, downloads_error=f"Could not prepare the downloads: {e}")
            if source["kind"] == "github" and (job_dir / "repo").exists():
                try:
                    _rmtree(job_dir / "repo")
                except OSError:
                    pass
            if kind == "tune":
                _update(job_id, finalized=True)  # downloads (if any) are ready


# --------------------------------------------------------------------------- AI training
def _run_tune(job_id: str, job_dir: Path, repo_dir: Path, runtime: str, params: dict, checkpoint: dict | None) -> None:
    if runtime != "node":
        raise RuntimeError("AI training is only available for JavaScript (Node.js) game modules for now. "
                           "Analysis works for both.")
    # The worker writes its checkpoint here. It gets its own folder because the worker may run as another user.
    out_dir = job_dir / "train"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / CHECKPOINT
    if checkpoint is not None:
        out_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    if worker_user_kwargs():
        shutil.chown(out_dir, WORKER_USER, WORKER_USER)
        if out_path.exists():
            shutil.chown(out_path, WORKER_USER, WORKER_USER)
    cmd = tune_command(repo_dir, out_path, params, resume=checkpoint is not None)
    _update(job_id, status="running", phase="load", runtime=runtime,
            message=f"Starting AI training on {tune_threads()} thread(s)")
    _stream(job_id, cmd, job_dir, TUNE_TIMEOUT, "Training complete")


REPLAY = "metafinder.replay.json"


def _read_worker_json(path: Path, max_mb: int) -> dict | None:
    """Reads a file the worker wrote, defensively: a regular file (not a link), not too large, a JSON object."""
    try:
        st = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > max_mb * 1024 * 1024:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _download_name(job_id: str) -> str:
    repo = _jobs[job_id]["repo"].rstrip("/").replace("\\", "/")
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", repo.rsplit("/", 1)[-1] or "game").strip("-.") or "game"


def _collect_training(job_id: str, job_dir: Path) -> None:
    """After a training run (finished, stopped, cancelled or failed): keeps the latest checkpoint and the
    tuned-vs-start replay game for download."""
    ck = _read_worker_json(job_dir / "train" / CHECKPOINT, 20)
    # Only progress made by this run counts (an uploaded checkpoint that was rejected isn't offered back).
    if ck is None or not isinstance(ck.get("params"), list) or not isinstance(ck.get("round"), int) \
            or ck["round"] <= (_jobs[job_id].get("resumed_from") or 0):
        return
    (job_dir / "checkpoint.json").write_text(json.dumps(ck, indent=2) + "\n", encoding="utf-8")
    params = [p for p in ck["params"] if isinstance(p, dict) and isinstance(p.get("path"), str)]
    check = ck.get("check") if isinstance(ck.get("check"), dict) else None
    downloads = {"checkpoint": True, "replay": False, "name": _download_name(job_id)}

    replay = _read_worker_json(job_dir / "train" / REPLAY, 50)
    if replay is not None and str(replay.get("format", "")).startswith("metafinder-replay/"):
        (job_dir / "replay.json").write_text(json.dumps(replay, indent=1) + "\n", encoding="utf-8")
        downloads["replay"] = True
    _update(job_id, downloads=downloads,
            training={"round": ck["round"], "rounds": ck.get("rounds"), "check": check,
                      "stopped_early": bool(ck.get("stopped_early")), "params": params,
                      "replay": {k: replay.get(k) for k in ("result", "players")} if downloads["replay"] else None})


# --------------------------------------------------------------------------- routes
def _start(source: dict, params: dict, kind: str, checkpoint: dict | None = None) -> dict:
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {"id": job_id, "kind": kind, "repo": source["display"], "status": "queued", "phase": "queue",
                         "pct": 0.0, "message": "Queued", "created": time.time(), "params": params,
                         "resumed_from": checkpoint.get("round") if checkpoint else None}
    threading.Thread(target=_run_job, args=(job_id, source, params, kind, checkpoint), daemon=True).start()
    return {"id": job_id}


@app.post("/api/jobs")
def create_job(req: AnalyzeRequest):
    source = parse_source(req.repo_url)
    return _start(source, req.model_dump(exclude={"repo_url"}), "analyze")


@app.post("/api/tune")
def create_tune_job(req: TuneRequest):
    source = parse_source(req.repo_url)
    ck = req.checkpoint
    if ck is not None:
        if len(json.dumps(ck)) > 20 * 1024 * 1024:
            raise HTTPException(400, "The saved progress file is too large")
        if not isinstance(ck.get("params"), list) or not isinstance(ck.get("theta"), list) \
                or not isinstance(ck.get("round"), int):
            raise HTTPException(400, f"That file isn't saved training progress (a {CHECKPOINT} from a training run)")
    return _start(source, req.model_dump(exclude={"repo_url", "checkpoint"}), "tune", ck)


def _job_file(job_id: str, name: str) -> Path:
    if job_id not in _jobs:
        raise HTTPException(404, "Unknown job")
    path = JOBS_DIR / job_id / name
    if not path.is_file():
        raise HTTPException(404, "Nothing to download yet")
    return path


@app.get("/api/jobs/{job_id}/checkpoint")
def download_checkpoint(job_id: str):
    """The training progress: upload it to a new training run to continue where this one stopped."""
    path = _job_file(job_id, "checkpoint.json")
    return FileResponse(path, media_type="application/json", filename=f"{_download_name(job_id)}.{CHECKPOINT}")


@app.get("/api/jobs/{job_id}/replay")
def download_replay(job_id: str):
    """One game of the tuned AI against the starting AI, move by move."""
    path = _job_file(job_id, "replay.json")
    return FileResponse(path, media_type="application/json", filename=f"{_download_name(job_id)}.{REPLAY}")


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return job


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    if job["status"] in ("done", "error", "cancelled"):
        return job
    _update(job_id, status="cancelled", message="Cancelled", finished=time.time())
    proc = _procs.get(job_id)
    if proc:
        _kill(proc)
    return _jobs[job_id]


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/examples")
def examples():
    if not EXAMPLES.is_dir():
        return []
    out = []
    for d in sorted(EXAMPLES.iterdir()):
        manifest = d / "metafinder.json"
        if manifest.is_file():
            out.append({"name": d.name, "path": f"examples/{d.name}"})
    return out


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
