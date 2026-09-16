FROM python:3.12-slim
WORKDIR /app
# No docker CLI. A shell in this image must not drive the host engine.
COPY pyproject.toml README.md LICENSE ./
COPY hestia ./hestia
COPY console ./console
RUN pip install --no-cache-dir .
RUN mkdir -p /data && chown 65532:65532 /data
ENV HOST=0.0.0.0 \
    HESTIA_HOST=0.0.0.0 \
    HESTIA_PORT=9480 \
    HESTIA_DATA_DIR=/data \
    HESTIA_RUNTIME=stub \
    HESTIA_ALLOW_HOST_DOCKER=0 \
    AIMARKET_PROVIDER_IDENTITY_FILE=/data/provider.key \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 65532:65532
VOLUME ["/data"]
EXPOSE 9480
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9480/health', timeout=3).read()"
CMD ["python", "-m", "hestia"]
