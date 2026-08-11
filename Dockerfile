# pokeflip - runs the scheduler, API and dashboard in one container.
FROM python:3.12-slim

# Unbuffered output so `docker logs` shows job activity as it happens.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    POKEFLIP_DATABASE=/data/pokeflip.db \
    POKEFLIP_NOTIFY__REPORT_DIR=/data/reports \
    POKEFLIP_HOST=0.0.0.0

WORKDIR /app

# Dependencies first so code edits do not invalidate the layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY pokeflip ./pokeflip
RUN pip install --no-cache-dir --no-deps -e .

# The database lives on a volume; the container itself stays disposable.
RUN useradd --create-home --uid 10001 pokeflip \
    && mkdir -p /data \
    && chown -R pokeflip:pokeflip /data /app
USER pokeflip
VOLUME ["/data"]
EXPOSE 8787

COPY deploy/healthcheck.py /app/healthcheck.py
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

# Scheduler + API + dashboard. Override with `pokeflip run` for jobs only.
CMD ["pokeflip", "serve"]
