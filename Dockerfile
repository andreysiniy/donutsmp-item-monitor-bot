FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

RUN addgroup --system bot \
    && adduser --system --ingroup bot --home /app bot

COPY --chown=bot:bot pyproject.toml README.md ./
COPY --chown=bot:bot src ./src
RUN pip install --no-cache-dir .

COPY --chown=bot:bot alembic.ini ./
COPY --chown=bot:bot migrations ./migrations
COPY --chown=bot:bot docker-entrypoint.sh ./
COPY --chown=bot:bot manifest_detailed.json ./assets/manifest_detailed.json
COPY --chown=bot:bot icons ./assets/icons

USER bot

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]

