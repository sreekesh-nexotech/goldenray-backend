# syntax=docker/dockerfile:1.7
# Flarize Platform backend images (PLAN §5.2). Build from the repository root:
#
#   docker build -f deploy/Dockerfile --target runtime   -t $IMAGE_REPO:$SHA .            # api, worker-default, beat
#   docker build -f deploy/Dockerfile --target documents -t $IMAGE_REPO-documents:$SHA .  # worker-documents
#
# builder   compiles every wheel once (build tools never reach a runtime image);
# runtime   slim Python + the wheels + the code, runs as the unprivileged `appuser` (uid 10001);
# documents runtime + headless Chromium (Playwright) + the Noto fonts the document templates declare
#           (Noto Sans, Noto Sans Malayalam, Noto Sans Devanagari — all in fonts-noto-core).
# Migrations never run on container start: deploy/release.sh runs them once, as the owner role.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements/base.txt requirements/base.txt
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements/base.txt


FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=flarize.settings.prod \
    PRIVATE_MEDIA_ROOT=/srv/flarize/media/private \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN groupadd --system --gid 10001 appuser \
 && useradd --system --uid 10001 --gid appuser --home-dir /app --shell /usr/sbin/nologin appuser
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl \
 && rm -rf /wheels
WORKDIR /app
# Code is root-owned and read-only for the app user; only runtime state is writable.
COPY . /app
RUN install -d -o appuser -g appuser /app/var /srv/flarize/media/private
USER appuser
EXPOSE 8000
# The api command; workers and beat override it in deploy/docker-compose.yml.
CMD ["gunicorn", "--config", "deploy/gunicorn.conf.py", "flarize.wsgi:application"]


FROM runtime AS documents
USER root
# Fonts first (cached layer), then Chromium headless shell with its system libraries.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-core fonts-noto-ui-core fonts-noto-color-emoji \
 && python -m playwright install --with-deps --only-shell chromium \
 && chown -R appuser:appuser /opt/pw-browsers \
 && rm -rf /var/lib/apt/lists/*
USER appuser
CMD ["celery", "-A", "flarize", "worker", "-Q", "documents", "-c", "2", "-n", "documents@%h", "--loglevel", "INFO"]
