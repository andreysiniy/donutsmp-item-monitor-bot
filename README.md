# DonutSMP Discord Monitor

Асинхронный Discord-бот отслеживает минимальную цену точного `item.id` на аукционе
DonutSMP и отправляет личное уведомление при пересечении пользовательского порога.
Штатный способ запуска — Docker Compose с PostgreSQL.

## Возможности

- `/auth` проверяет личный DonutSMP Bearer-токен и сохраняет только его Fernet-шифротекст;
- `/watch add|list|delete|pause|resume` управляет правилами снижения и повышения цены;
- `/price` выполняет разовую проверку без создания правила;
- `/status` показывает авторизацию, правила, API-бюджет, доступность API и ошибки DM;
- точное совпадение `minecraft:*`, цена лота или единицы, `Decimal` без `float`;
- защита от повторов: конечный автомат, hysteresis 2% и cooldown 60 секунд;
- дедупликация запросов по токену, предмету и типу цены;
- sliding-window лимит 220 запросов/мин с резервом 30 интерактивных запросов;
- отдельный backoff каждого токена для `429`, `5xx` и сетевых ошибок;
- иконки блоков и предметов из приложенного Minecraft-манифеста;
- состояние правил, наблюдения и доставка уведомлений сохраняются в PostgreSQL.

## Архитектура

```text
src/donutsmp_bot/
├── core/            конфигурация, общие enum, шифрование и безопасные логи
├── domain/          модели ответа API и чистый автомат пересечения порога
├── application/     use cases и планировщик групп запросов по токенам
├── infrastructure/  DonutSMP HTTP-клиент, rate limiter и индекс иконок
├── persistence/     SQLAlchemy-модели, сессии и репозитории
├── presentation/    Discord-команды, modal, views, embeds и lifecycle
└── main.py          composition root
```

Зависимости направлены внутрь: доменная логика не знает о Discord, HTTP или базе
данных. Ошибка одной token-группы не останавливает другие группы.

## Подготовка Discord

1. Создайте приложение и бота в Discord Developer Portal.
2. Пригласите бота со scopes `bot` и `applications.commands`.
3. Скопируйте bot token в локальный `.env`. Не публикуйте его и не добавляйте `.env`
   в Git.
4. Пользовательский DonutSMP-токен вводится только через ephemeral-flow `/auth`.

Message Content Intent не требуется.

## Запуск в Docker

Требуются Docker Engine и Docker Compose v2.

```bash
cp .env.example .env
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Заполните в `.env`:

```env
DISCORD_BOT_TOKEN=<Discord bot token>
TOKEN_ENCRYPTION_KEY=<результат команды выше>
POSTGRES_PASSWORD=<случайный пароль для PostgreSQL>
```

Затем:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f bot
```

Контейнер бота ждёт healthcheck PostgreSQL, автоматически выполняет
`alembic upgrade head`, затем запускает приложение под непривилегированным
пользователем на read-only filesystem. Данные PostgreSQL хранятся в named volume
`postgres_data`.

Остановка без удаления данных:

```bash
docker compose down
```

Удаление окружения вместе с базой:

```bash
docker compose down -v
```

Последняя команда необратимо удаляет PostgreSQL volume.

## Настройки

Все настройки читаются из переменных окружения. Полный безопасный шаблон находится
в `.env.example`. В Docker `DATABASE_URL`, `MANIFEST_PATH` и `ASSETS_DIR`
устанавливаются `compose.yaml`; секреты остаются в `.env`.

Основные значения:

| Переменная | По умолчанию | Назначение |
|---|---:|---|
| `SAFE_REQUESTS_PER_MINUTE` | `220` | бюджет фонового мониторинга на токен |
| `RESERVED_REQUESTS_PER_MINUTE` | `30` | резерв `/price`, `/auth` и ручных действий |
| `DEFAULT_POLL_INTERVAL_SECONDS` | `3` | минимальный интервал |
| `MAX_SEARCH_PAGES` | `3` | максимальное число страниц поиска |
| `DEFAULT_HYSTERESIS_PERCENT` | `2` | зона повторного взведения правила |
| `DEFAULT_NOTIFICATION_COOLDOWN_SECONDS` | `60` | cooldown одного правила |

## Проверки

Локально, если нужен запуск тестов вне контейнера:

```bash
python -m pip install -e ".[dev]"
pytest
ruff check .
```

Миграции проверяются командой:

```bash
alembic upgrade head
alembic downgrade base
alembic upgrade head
```

## Безопасность

- исходные токены не записываются в таблицы, логи, embeds и сообщения об ошибках;
- ключ шифрования хранится отдельно от базы в окружении;
- HTTP-заголовок Authorization создаётся только на время запроса конкретного
  пользователя;
- уведомления отправляются только владельцу соответствующего токена;
- при `401/403` токен инвалидируется, правила останавливаются, одно DM просит
  повторить `/auth`;
- закрытые DM фиксируются в БД и показываются владельцу через `/status`;
- публичная отправка уведомлений и автоматическая покупка не реализованы.

