FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN addgroup --system bot \
    && adduser --system --ingroup bot --home /app bot

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY migrations ./migrations
COPY docker-entrypoint.sh ./
COPY manifest_detailed.json ./assets/manifest_detailed.json
COPY icons ./assets/icons

RUN chown -R bot:bot /app

USER bot

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]

