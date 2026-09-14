# Optional administrator-only image; full-database transfer is never a public API.
FROM medical-app-aliyun-api:v3
USER 0:0
COPY tools/aliyun_platform/requirements-transfer-linux-lock.txt /tmp/transfer-lock.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/transfer-lock.txt && python -m pip check
COPY server/platform_transfer.py server/platform_transfer_assets.py server/platform_transfer_package.py /app/server/
COPY tools/aliyun_platform/transfer.py /app/tools/aliyun_platform/transfer.py
CMD ["python", "-m", "tools.aliyun_platform.transfer", "--help"]
