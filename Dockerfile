FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first: this layer is rebuilt only when pyproject.toml changes,
# not on every source edit. README is required because pyproject declares it.
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# Migrations must exist inside the image. Without these, "alembic upgrade head"
# cannot run in the deployment environment, which is exactly what the compose
# stack's migrate service and any release job depend on.
COPY alembic.ini ./
COPY alembic ./alembic

# Non-root runtime user. The application needs no write access to its own code,
# no shell, and no capabilities: it opens a listening socket and talks to Postgres.
RUN useradd --create-home --uid 10001 kai && chown -R kai:kai /app
USER kai

EXPOSE 8000

# Reports the real readiness signal, not just "the process is alive":
# /api/v1/health verifies database reachability and reports 200 only when the
# database answers. A liveness-only probe would report a broken deployment healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"

# Startup is deliberately NOT "migrate then serve" inside the app container.
# Alembic migrations take locks on the same schema the running replicas use, so
# running them per-replica on boot turns a rolling deploy into a race. Migrations
# are applied once, by a dedicated one-shot job (see docker-compose.yml).
CMD ["uvicorn", "kai.main:app", "--host", "0.0.0.0", "--port", "8000"]
