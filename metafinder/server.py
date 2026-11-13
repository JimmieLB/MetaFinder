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


def worker_command(runtime: str, repo_dir: Path, params_path: Path) -> list[str]:
    if runtime == "node":
        node = os.environ.get("METAFINDER_NODE") or shutil.which("node")
        if not node:
            raise RuntimeError("This game is a Node.js module, but Node.js isn't installed on the MetaFinder host "
                               "(install Node 20+ or set METAFINDER_NODE to its path)")
        cmd = [node, f"--max-old-space-size={WORKER_MEMORY_MB}", str(ROOT / "metafinder" / "node" / "worker.mjs"),
               "--repo", str(repo_dir), "--params", str(params_path)]
    else:
        cmd = [sys.executable, "-m", "metafinder.worker", "--repo", str(repo_dir), "--params", str(params_path)]
    bash = shutil.which("bash") if POSIX else None
    if bash:
        # Resource limits for the submitted code: CPU seconds, file size (KB), processes/threads for the
        # worker's user, and (Python only; V8 reserves far more address space than it uses) memory.
        limits = f"ulimit -t {JOB_TIMEOUT + 60} && ulimit -f 262144 && ulimit -u 512"
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


def _run_job(job_id: str, source: dict, params: dict) -> None:
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    _update(job_id, status="queued", message="Waiting for a free worker slot")
    with _slots:
        if _jobs[job_id]["status"] == "cancelled":
            return
        try:
            # ---- fetch
            if source["kind"] == "github":
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
            else:
                repo_dir = Path(source["path"])

            # ---- analyze
            params_path = job_dir / "params.json"
            # The worker paces itself to finish (with partial results if need be) before the hard kill.
            worker_params = {**params, "time_budget": max(30, JOB_TIMEOUT - 20)}
            params_path.write_text(json.dumps(worker_params), encoding="utf-8")
            env = worker_env()
            runtime = detect_runtime(repo_dir)
            cmd = worker_command(runtime, repo_dir, params_path)
            _update(job_id, status="running", phase="load", runtime=runtime,
                    message=f"Starting analysis worker ({'Node.js' if runtime == 'node' else 'Python'})")
            with open(job_dir / "worker.log", "w", encoding="utf-8") as log:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(job_dir), env=env, stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8",
                    start_new_session=POSIX, **worker_user_kwargs(),
                )
                _procs[job_id] = proc
                timed_out = threading.Event()
                killer = threading.Timer(JOB_TIMEOUT, lambda: (timed_out.set(), _kill(proc)))
                killer.start()
                got_result = False
                try:
                    for line in proc.stdout:
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if msg["type"] == "progress":
                            _update(job_id, phase=msg["phase"], pct=msg["pct"], message=msg["message"])
                        elif msg["type"] == "result":
                            got_result = True
                            _update(job_id, status="done", phase="done", pct=1, message="Analysis complete",
                                    result=msg, finished=time.time())
                        elif msg["type"] == "error":
                            got_result = True
                            _update(job_id, status="error", message=msg["message"], traceback=msg.get("traceback"),
                                    finished=time.time())
                    proc.wait()
                finally:
                    killer.cancel()
                    if POSIX:
                        _kill(proc)  # anything the game code left running in the worker's process group
                    _procs.pop(job_id, None)

            if _jobs[job_id]["status"] == "cancelled":
                return
            if not got_result:
                tail = (job_dir / "worker.log").read_text(encoding="utf-8", errors="replace")[-2000:]
                reason = f"timed out after {JOB_TIMEOUT}s; try a smaller budget" if timed_out.is_set() \
                    else f"exited with code {proc.returncode}"
                _update(job_id, status="error", message=f"Analysis worker {reason}", traceback=tail,
                        finished=time.time())
        except subprocess.TimeoutExpired:
            _update(job_id, status="error", message="git clone timed out", finished=time.time())
        except Exception as e:
            _update(job_id, status="error", message=str(e), finished=time.time())
        finally:
            if source["kind"] == "github" and (job_dir / "repo").exists():
                try:
                    _rmtree(job_dir / "repo")
                except OSError:
                    pass


# --------------------------------------------------------------------------- routes
@app.post("/api/jobs")
def create_job(req: AnalyzeRequest):
    source = parse_source(req.repo_url)
    job_id = uuid.uuid4().hex[:12]
    params = req.model_dump(exclude={"repo_url"})
    with _lock:
        _jobs[job_id] = {"id": job_id, "repo": source["display"], "status": "queued", "phase": "queue",
                         "pct": 0.0, "message": "Queued", "created": time.time(), "params": params}
    threading.Thread(target=_run_job, args=(job_id, source, params), daemon=True).start()
    return {"id": job_id}


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
