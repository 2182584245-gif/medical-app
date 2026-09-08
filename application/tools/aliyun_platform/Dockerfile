# Source root is the build context. Adjacent deny-all allowlist is mandatory.
FROM python:3.13.15-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONPATH=/app:/app/src
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates tzdata && apt-get clean
COPY tools/local_platform/requirements-linux-lock.txt /app/requirements-linux-lock.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /app/requirements-linux-lock.txt && python -m pip check
COPY server/ /app/server/
COPY src/ /app/src/
COPY tools/aliyun_platform/ /app/tools/aliyun_platform/
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app
USER 10001:10001
RUN python -c "import server.platform_app; import tools.aliyun_platform.bootstrap; import tools.aliyun_platform.api_entrypoint"
EXPOSE 8443
CMD ["python", "-m", "tools.aliyun_platform.api_entrypoint"]
