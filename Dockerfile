# Official Playwright image already has Chromium + every OS-level
# dependency it needs pre-installed — avoids hand-rolling a long apt-get
# list that drifts out of date every time Playwright bumps its browser build.
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

ENV PORT=8080
# Python buffers stdout/stderr by default when not attached to a TTY —
# in a container that means log lines can sit in a buffer and never
# reach the platform's log collector. This forces unbuffered I/O so
# logger.error() calls actually show up in Render's Logs tab.
ENV PYTHONUNBUFFERED=1
EXPOSE 8080

# Single worker: this is running on a 512MB free-tier instance, and each
# concurrent Chromium launch can use 200-300MB+ on its own. Running 2
# workers meant 2 possible simultaneous Chromium processes, which was
# almost certainly what pushed memory over the limit and got the whole
# container OOM-killed in a restart loop. One worker means requests queue
# instead of running in parallel — slower under load, but it won't crash.
CMD ["gunicorn", "--workers=1", "--threads=1", "--timeout=90", "--bind=0.0.0.0:8080", "app:app"]
