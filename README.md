# DonutSMP Discord Monitor

An asynchronous Discord bot that monitors the lowest price for an exact `item.id`
on the DonutSMP auction and sends a direct message when a user-defined threshold
is crossed. The standard deployment uses Docker Compose and PostgreSQL.

## Features

- `/auth` validates a personal DonutSMP Bearer token and stores only its encrypted value;
- `/watch add|list|delete|pause|resume` manages price-drop and price-rise rules;
- `/price` performs a one-time price check without creating a rule;
- `/status` shows authorization, active rules, API budget, API health, and DM errors;
- exact `minecraft:*` matching, whole-listing or per-item prices, and `Decimal` arithmetic;
- duplicate-notification protection with a state machine, 2% hysteresis, and cooldown;
- request deduplication by token, item, and price type;
- a 220-request sliding-window monitoring budget with 30 interactive requests reserved;
- isolated per-token backoff for `429`, `5xx`, timeout, and connection errors;
- block and item icons loaded from the supplied Minecraft manifest;
- PostgreSQL persistence for users, rules, observations, and notification delivery.

## Architecture

```text
src/donutsmp_bot/
├── core/            configuration, shared enums, encryption, and safe logging
├── domain/          API response models and pure threshold state machine
├── application/     use cases and per-token monitoring orchestration
├── infrastructure/  DonutSMP HTTP client, rate limiter, and icon index
├── persistence/     SQLAlchemy models, sessions, and repositories
├── presentation/    Discord commands, modals, views, embeds, and lifecycle
└── main.py          composition root
```

Dependencies point inward: the domain layer does not know about Discord, HTTP, or
the database. A failure in one token group does not stop any other group.

## Discord Setup

1. Create an application and bot in the Discord Developer Portal.
2. Invite the bot with the `bot` and `applications.commands` scopes.
3. Copy the bot token into a local `.env`. Never publish it or add `.env` to Git.
4. Each user submits their personal DonutSMP token through the ephemeral `/auth` flow.

Message Content Intent is not required.

## Docker Deployment

Docker Engine and Docker Compose v2 are required.

```bash
cp .env.example .env
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set these values in `.env`:

```env
DISCORD_BOT_TOKEN=<Discord bot token>
TOKEN_ENCRYPTION_KEY=<output of the command above>
POSTGRES_PASSWORD=<output of the second command>
```

Start the application:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f bot
```

The bot container waits for the PostgreSQL healthcheck, runs `alembic upgrade head`,
and starts the application as an unprivileged user on a read-only filesystem.
PostgreSQL data is stored in the `postgres_data` named volume.

Stop the application without deleting data:

```bash
docker compose down
```

Delete the environment and its database:

```bash
docker compose down -v
```

The final command permanently deletes the PostgreSQL volume.

### Database Password Recovery

PostgreSQL applies `POSTGRES_PASSWORD` only when it initializes a new data volume.
Changing the value in `.env` later does not update the password of the existing
database role. The authenticated PostgreSQL healthcheck detects this mismatch
before the bot starts.

If the database contains data that must be preserved, update the role interactively:

```bash
docker compose exec postgres psql -U donutsmp -d donutsmp -c '\password donutsmp'
```

Enter the current `POSTGRES_PASSWORD` value from `.env` twice, then recreate the
services without deleting the volume:

```bash
docker compose up -d --build --force-recreate
```

If the database is disposable, initialize a new volume instead:

```bash
docker compose down -v
docker compose up -d --build
```

Never use `down -v` when the existing database must be retained.

## Configuration

All settings are read from environment variables. `.env.example` contains the full
safe template. Docker sets `DATABASE_URL`, `MANIFEST_PATH`, and `ASSETS_DIR` in
`compose.yaml`; secrets remain in `.env`.

| Variable | Default | Purpose |
|---|---:|---|
| `SAFE_REQUESTS_PER_MINUTE` | `220` | background monitoring budget per token |
| `RESERVED_REQUESTS_PER_MINUTE` | `30` | reserve for `/price`, `/auth`, and user actions |
| `DEFAULT_POLL_INTERVAL_SECONDS` | `3` | minimum polling interval |
| `MAX_SEARCH_PAGES` | `3` | maximum auction pages per search |
| `DEFAULT_HYSTERESIS_PERCENT` | `2` | rule rearming margin |
| `DEFAULT_NOTIFICATION_COOLDOWN_SECONDS` | `60` | per-rule notification cooldown |

## Quality Checks

For local checks outside Docker:

```bash
python -m pip install -e ".[dev]"
pytest
ruff check .
mypy src
```

Verify migrations with:

```bash
alembic upgrade head
alembic downgrade base
alembic upgrade head
```

## Security

- raw tokens are never written to tables, logs, embeds, or error messages;
- the encryption key is stored separately from the database in the environment;
- the Authorization header exists only for a request made with its owner's token;
- notifications are sent only to the Discord owner of the corresponding token;
- `401/403` invalidates the token, pauses its rules, and sends one reauthorization DM;
- closed DMs are recorded and shown to the owner through `/status`;
- public-channel notifications and automatic purchasing are intentionally unsupported.
