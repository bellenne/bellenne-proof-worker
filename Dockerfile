FROM python:3.12-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    WORKER_DATA_PATH=/data WORKER_OUTPUT_PATH=/output TMPDIR=/data/tmp VIPS_CONCURRENCY=2
RUN apt-get update && apt-get install -y --no-install-recommends libvips42 libgomp1 ca-certificates fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 proof && useradd --uid 10001 --gid proof --no-create-home proof \
    && mkdir -p /data/tmp /output /sources/main && chown -R proof:proof /data /output
WORKDIR /opt/proof-worker
COPY requirements.txt pyproject.toml ./
RUN pip install -r requirements.txt
COPY app ./app
RUN pip install --no-deps .

FROM base AS test
COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY tests ./tests
CMD ["python", "-m", "pytest", "-q"]

FROM base AS runtime
USER proof
VOLUME ["/data", "/output"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 CMD ["python", "-m", "app.health"]
CMD ["python", "-m", "app.main"]
