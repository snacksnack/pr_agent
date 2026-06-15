# PR Review Agent — container image for the live webhook service (RC1-119).
#
# Runs the FastAPI receiver (app/webhook.py) under uvicorn. The agentic review
# loop runs in-process on a background task, so a single small machine is enough;
# Fly auto-stops it when idle (see fly.toml).
FROM python:3.12-slim

# - PYTHONUNBUFFERED: stream logs straight to stdout so `fly logs` is live.
# - PYTHONDONTWRITEBYTECODE: no .pyc clutter in the image.
# - PIP_NO_CACHE_DIR: smaller image, no wheel cache layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first so the layer is cached across code-only changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App package only — tests, .env, and the venv are excluded via .dockerignore.
COPY app ./app

# Run as a non-root user (defense in depth; the service needs no root).
RUN useradd --create-home --uid 10001 appuser
USER appuser

# Fixed port; matches fly.toml's internal_port.
EXPOSE 8080

# Exec form so uvicorn is PID 1 and receives SIGTERM for a graceful shutdown.
# One worker: reviews are bounded background tasks and a re-push is deduped, so a
# single process keeps the in-memory dedup state coherent.
CMD ["uvicorn", "app.webhook:app", "--host", "0.0.0.0", "--port", "8080"]
