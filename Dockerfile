# Official Playwright image already has Chromium + every OS-level
# dependency it needs pre-installed — avoids hand-rolling a long apt-get
# list that drifts out of date every time Playwright bumps its browser build.
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

ENV PORT=8080
EXPOSE 8080

# 2 workers, 1 thread each: Playwright launches a real Chromium process
# per request, which is memory-heavy — keep concurrency low on a small
# VM rather than risk OOM from too many workers.
CMD ["gunicorn", "--workers=2", "--threads=1", "--timeout=60", "--bind=0.0.0.0:8080", "app:app"]
