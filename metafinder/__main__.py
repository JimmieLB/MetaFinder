import argparse
import os
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    """Reads KEY=value lines from a .env file. Variables already set in the environment win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


ap = argparse.ArgumentParser(description="Run the MetaFinder web app")
ap.add_argument("--host", default=None, help="default: $HOST, else 127.0.0.1")
ap.add_argument("--port", type=int, default=None, help="default: $PORT (set by Railway and similar hosts), else 8000")
ap.add_argument("--env-file", default=str(ROOT / ".env"), help="settings file (see example.env)")
args = ap.parse_args()
load_env(Path(args.env_file))  # before the server module reads its settings
host = args.host or os.environ.get("HOST", "127.0.0.1")
port = args.port or int(os.environ.get("PORT", 8000))

# Analysis runs code from the submitted repositories: never expose that to the internet without a password.
if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("METAFINDER_PASSWORD") \
        and os.environ.get("METAFINDER_ALLOW_PUBLIC") != "1":
    raise SystemExit(
        f"Refusing to listen on {host} without METAFINDER_PASSWORD: anyone who can reach the server could run "
        "arbitrary code on it. Set METAFINDER_PASSWORD (or METAFINDER_ALLOW_PUBLIC=1 if you accept that risk)."
    )
uvicorn.run("metafinder.server:app", host=host, port=port, proxy_headers=True, forwarded_allow_ips="*")
