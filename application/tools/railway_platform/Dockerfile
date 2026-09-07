# syntax=docker/dockerfile:1
# Build context: the repository's /application directory, NOT this directory.
# Dockerfile.dockerignore is the adjacent, deny-by-default context allowlist.
FROM python:3.13.15-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    PORT=8080

WORKDIR /app

# Never install requirements.txt, requirements-lock.txt, or this desktop project
# with "pip install .": those bring in Qt/OCR/voice and desktop-only dependencies.
COPY requirements-server.txt /app/requirements-server.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: -r /app/requirements-server.txt \
    && python -m pip check

# All code copied here is filtered by the adjacent Dockerfile.dockerignore.
COPY server/ /app/server/
COPY src/ /app/src/

# Runtime cannot modify application code. The certificate is provided at runtime
# by platform_entrypoint through a mode-0600 file in /tmp, not a build argument.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/app \
        --shell /usr/sbin/nologin app
USER 10001:10001

EXPOSE 8080
CMD ["python", "-m", "server.platform_entrypoint"]
