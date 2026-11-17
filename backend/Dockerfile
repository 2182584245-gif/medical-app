FROM python:3.13.15-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app:/app/src
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-linux-lock.txt ./
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements-linux-lock.txt \
    && python -m pip check
COPY server/ server/
COPY expansion/ expansion/
COPY src/ src/
COPY data/nutrition-catalog/ data/nutrition-catalog/
RUN groupadd --gid 10001 fawnsee \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin fawnsee
USER 10001:10001
RUN python -c "from server.platform_app import create_app; from server.record_tasks11864 import CAPABILITY"
EXPOSE 8443
ENTRYPOINT ["python", "-m", "uvicorn", "server.platform_app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8443", "--no-proxy-headers", "--no-access-log"]
CMD ["--ssl-certfile", "/run/fawnsee/tls/server.crt", "--ssl-keyfile", "/run/fawnsee/tls/server.key"]
