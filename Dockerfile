# MetaFinder: Python server + Python and Node.js analysis workers.
FROM python:3.12-slim-bookworm

# Node.js for JavaScript game modules (official binary, no npm needed), git for cloning repositories.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --no-create-home --shell /usr/sbin/nologin runner

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY metafinder ./metafinder
COPY examples ./examples

# The server runs as root only so it can start each analysis as the unprivileged `runner` user,
# which can't read the server's environment (METAFINDER_PASSWORD and Railway's variables).
ENV HOST=0.0.0.0 \
    PORT=8000 \
    METAFINDER_ALLOW_LOCAL=0 \
    METAFINDER_WORKER_USER=runner \
    METAFINDER_JOBS_DIR=/tmp/metafinder-jobs \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000
CMD ["python", "-m", "metafinder"]
