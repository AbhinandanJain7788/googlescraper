# Railway uses this Dockerfile to build the scraper image.
#
# Why Dockerfile and not nixpacks?
#   Playwright's Chromium needs ~30 system libs (libnss3, libatk-bridge, etc.).
#   The Playwright team maintains a base image with them pre-installed, which
#   saves both build time AND the per-build minute cost on Railway's free tier.
#   Building from python:slim and installing libs by hand takes ~3 min per build
#   and breaks every time Playwright bumps a dep; the official image is faster
#   and more reliable.

FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

# Avoid pyc files and force flushed stdout/stderr so Railway logs are usable.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy requirements first so Docker layer caching reuses the pip-install layer
# across deploys when only application code changed.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# The Playwright image already ships Chromium. We don't reinstall it.

COPY . .

# Mount a persistent volume here on Railway. Without a mount it still works
# (writes to the container's ephemeral disk), but jobs.db and result files
# get wiped on every redeploy.
ENV DATA_DIR=/data
RUN mkdir -p /data

# Railway injects $PORT. Default to 8000 for local `docker run`.
ENV PORT=8000
EXPOSE 8000

# Use shell form so $PORT expands at container runtime, not at image build.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
