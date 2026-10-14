FROM python:3.12-slim
WORKDIR /app
# No docker CLI. A shell in this image must not drive the host engine.
COPY pyproject.toml README.md LICENSE ./
COPY hestia ./hestia
COPY console ./console
COPY docs/landing ./docs/landing
COPY scripts/fetch_python_wasi.py ./scripts/fetch_python_wasi.py
RUN pip install --no-cache-dir ".[postgres,pqc,wasm]"
# CPython for WASI (pinned by SHA-256): what the sandbox runner executes handlers in.
RUN python scripts/fetch_python_wasi.py /opt/python-wasi
# /run/hestia-runner seeds the socket volume the hearth and the runner share, owned by the
# unprivileged user both run as.
RUN mkdir -p /data /run/hestia-runner && chown 65532:65532 /data /run/hestia-runner
ENV HOST=0.0.0.0 \
    HESTIA_HOST=0.0.0.0 \
    HESTIA_PORT=9480 \
    HESTIA_DATA_DIR=/data \
    HESTIA_RUNTIME=stub \
    HESTIA_WASM_ROOT=/opt/python-wasi \
    HESTIA_ALLOW_HOST_DOCKER=0 \
    HESTIA_PQC=1 \
    AIMARKET_PROVIDER_IDENTITY_FILE=/data/provider.key \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 65532:65532
VOLUME ["/data"]
EXPOSE 9480
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9480/health', timeout=3).read()"
CMD ["python", "-m", "hestia"]
