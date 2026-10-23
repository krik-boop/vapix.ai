# ai-business-os

Сервис только для чтения: забирает данные из **amoCRM API v4** и **API hh.ru (кабинет работодателя)** и складывает их в PostgreSQL.

Стек: Python 3.12, FastAPI, PostgreSQL, SQLAlchemy 2.0, Alembic, Docker Compose, pytest, httpx.

## Структура

```
app/
  core/            настройки (.env), подключение к БД + upsert, логирование с маскировкой секретов
  integrations/
    http.py        общий HTTP-клиент: только GET, ≤5 запросов/с, повторы при 429/5xx
    sync_state.py  курсоры инкрементальной синхронизации (таблица sync_state)
    amo/           клиент amoCRM v4 + синхронизация
    hh/            клиент hh.ru + OAuth + синхронизация
  models/          модели SQLAlchemy (amo_*, hh_*, sync_state)
  cli/             команды синхронизации
  main.py          FastAPI: /health, /sync/status
migrations/        Alembic
tests/             pytest, оба API замоканы (httpx.MockTransport), БД — SQLite in-memory
```

## Запуск через Docker Compose

```bash
cp .env.example .env          # заполнить значения, см. ниже
docker compose up -d --build  # поднимает postgres и app; app сам выполняет `alembic upgrade head`
curl http://127.0.0.1:8000/health
```

Синхронизация запускается вручную (или из cron / планировщика):

```bash
docker compose run --rm app python -m app.cli amo sync full
docker compose run --rm app python -m app.cli amo sync incremental
docker compose run --rm app python -m app.cli hh sync full
docker compose run --rm app python -m app.cli hh sync incremental
```

То же самое доступно как `ai-business-os amo sync full` после `pip install .`.
Код выхода: `0` — успешно, `1` — ошибка API или сущности, `2` — ошибка конфигурации.

Статус последних запусков: `GET http://127.0.0.1:8000/sync/status`.

Тесты:

```bash
docker compose run --rm app pytest
# или локально
pip install ".[dev]" && pytest
```

## Как заполнить `.env`

`.env` указан в `.gitignore` и `.dockerignore` — в репозиторий и в образ он не попадает. Все секреты читаются только из переменных окружения.

| Переменная | Что указать |
|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | учётные данные БД; ими же инициализируется контейнер `db` |
| `POSTGRES_HOST`, `POSTGRES_PORT` | в compose хост принудительно `db`; при локальном запуске — свой |
| `DATABASE_URL` | необязательно; полный URL SQLAlchemy, заменяет `POSTGRES_*` |
| `AMO_DOMAIN` | домен аккаунта без `https://`, например `yourcompany.amocrm.ru` |
| `AMO_TOKEN` | долгосрочный токен: amoCRM → Настройки → Интеграции → ваша интеграция → «Ключи и доступы» → «Долгосрочный токен». Чтобы читать `/api/v4/users`, нужен токен пользователя-администратора |
| `HH_CLIENT_ID`, `HH_CLIENT_SECRET` | данные приложения с dev.hh.ru; нужны только для обновления токена |
| `HH_ACCESS_TOKEN`, `HH_REFRESH_TOKEN` | токены работодателя, полученные по OAuth (authorization_code → `POST https://api.hh.ru/token`). Сервис этот обмен не выполняет |
| `HH_USER_AGENT` | обязательный заголовок hh.ru: `AppName/1.0 (контактный-email)` |
| `HH_EMPLOYER_ID` | ID компании на hh.ru. Рекомендуем указать (см. TODO про `/me`) |
| `HH_VACANCIES_ALL_ACCESSIBLE` | `true` — брать активные вакансии всех доступных менеджеров (`all_accessible=true`) |
| `HH_TOKEN_AUTO_REFRESH` | `false` по умолчанию. Подробности в разделе про OAuth hh.ru |
| `HTTP_RPS` | лимит запросов в секунду, не больше 5 (больше не пропустит валидация) |
| `HTTP_MAX_RETRIES`, `HTTP_BACKOFF_BASE`, `HTTP_BACKOFF_MAX`, `HTTP_TIMEOUT` | повторы и таймауты |
| `SYNC_OVERLAP_SECONDS` | инкрементальная синхронизация перечитывает данные с запасом в N секунд до курсора |

## Общие правила клиентов

- **Только GET.** `ReadOnlyClient` (`app/integrations/http.py`) умеет отправлять только GET. Кроме того, request-hook httpx выбрасывает `ReadOnlyViolation` на любой другой метод. Абсолютные URL на чужой хост отклоняются, чтобы токен не утёк. Единственное исключение — необязательное обновление OAuth-токена hh.ru (см. ниже).
- **Не больше 5 запросов в секунду** на каждый клиент: между запросами выдерживается пауза не меньше 0,2 с.
- **Повторы при 429, 5xx и сетевых ошибках.** Пауза растёт экспоненциально: `base·2^n`, но не дольше `HTTP_BACKOFF_MAX`. Если сервер прислал `Retry-After`, он учитывается. Остальные 4xx не повторяются.
- **Пагинация** проходит все страницы. amoCRM: `page` с 1, `limit=250`, пока есть `_links.next`; ответ `204 No Content` означает пустой результат. hh.ru: `page` с 0, пока `page + 1 < pages`.
- **Инкрементальная синхронизация.** В `sync_state` для каждой сущности хранится курсор — максимальная дата изменения из уже полученных данных (время сервера). При `incremental` запрашиваются данные начиная с `курсор − SYNC_OVERLAP_SECONDS`. Запись идёт через upsert (`INSERT … ON CONFLICT DO UPDATE`), поэтому повторное чтение безопасно. Без курсора `incremental` работает как `full`. Ошибка в одной сущности записывается в `sync_state` и не останавливает остальные.
- **Секреты в логах.** Фильтр маскирует значения всех секретных настроек (токены, ключи, домен amo, пароль БД), строки вида `Bearer …` и `access_token=…`, пароль в DSN и текст трейсбеков. Логгеры httpx/httpcore переведены на уровень WARNING. В SQLAlchemy включён `hide_parameters`.

## Использованные эндпоинты

### amoCRM API v4 (`https://{AMO_DOMAIN}`, `Authorization: Bearer {AMO_TOKEN}`)

| Сущность | Запрос | Инкремент | Документация |
|---|---|---|---|
| users | `GET /api/v4/users?page&limit=250` | нет, всегда полностью | [users-api](https://www.amocrm.ru/developers/content/crm_platform/users-api) |
| pipelines + statuses | `GET /api/v4/leads/pipelines` (статусы в `_embedded.statuses`) | нет, всегда полностью | [leads_pipelines](https://www.amocrm.ru/developers/content/crm_platform/leads_pipelines) |
| leads | `GET /api/v4/leads?with=loss_reason&order[updated_at]=asc&filter[updated_at][from]=ts` | `filter[updated_at][from]` | [leads-api](https://www.amocrm.ru/developers/content/crm_platform/leads-api), [filters-api](https://www.amocrm.ru/developers/content/crm_platform/filters-api) |
| tasks | `GET /api/v4/tasks?order[id]=asc&filter[updated_at][from]=ts` | `filter[updated_at][from]` | [tasks-api](https://www.amocrm.ru/developers/content/crm_platform/tasks-api) |
| notes (сделок) | `GET /api/v4/leads/notes?order[updated_at]=asc&filter[updated_at]=ts` | `filter[updated_at]` | [events-and-notes](https://www.amocrm.ru/developers/content/crm_platform/events-and-notes) |
| loss_reasons | из `_embedded.loss_reason` сделок (`with=loss_reason`) | вместе со сделками | [leads-api](https://www.amocrm.ru/developers/content/crm_platform/leads-api) |

Коды 429 («превышено количество запросов в секунду») и 204 («ничего не найдено») описаны в [error-codes](https://www.amocrm.ru/developers/content/crm_platform/error-codes).

### hh.ru (`https://api.hh.ru`, `Authorization: Bearer …`, `HH-User-Agent`)

Всё сверено с официальной OpenAPI-спецификацией: https://api.hh.ru/openapi/specification/public.

| Сущность | Запрос | Примечание |
|---|---|---|
| employer id | `GET /me` | только если `HH_EMPLOYER_ID` пуст (см. TODO) |
| активные вакансии | `GET /employers/{employer_id}/vacancies/active?page&per_page=50` | `per_page` максимум 50 |
| архивные вакансии | `GET /employers/{employer_id}/vacancies/archived?page&per_page=100` | только в режиме `full` |
| коллекции откликов | `GET /negotiations?vacancy_id=…` | возвращает `collections[].url` |
| отклики | `GET {collections[].url}&page&per_page=50` (например `/negotiations/response?vacancy_id=…`) | URL берётся из `collections[].url`, как требует документация |
| (опционально) токен | `POST /token` (`grant_type=refresh_token`) | только при `HH_TOKEN_AUTO_REFRESH=true` |

Данные кандидата берутся из короткого резюме (`items[].resume`), которое приходит в списке откликов. Полное резюме (`GET /resumes/{id}`) не запрашивается: по документации для работодателя этот метод требует платного доступа и может расходовать просмотры.

### Какие данные кандидатов хранятся

Таблица `hh_candidates` содержит только: `resume_id`, город (`area.name`), желаемую зарплату (`salary.amount/currency`), общий опыт в месяцах, опыт работы (только `position`, `company`, `industry`, `start`, `end`; описание обязанностей не сохраняется) и ключевые навыки (`skill_set`).
Для ФИО, фото, возраста/даты рождения, пола, телефона, email и других контактов в схеме **нет колонок**. Маппинг работает по белому списку полей, это проверяется тестом `test_candidate_pii_is_not_stored`.
Email пользователей amoCRM тоже не сохраняется.

### OAuth hh.ru и требование «только GET»

Срок жизни access token в hh.ru ограничен, и обновить его можно только запросом `POST https://api.hh.ru/token`. Этот POST противоречит правилу «только GET», поэтому:

- по умолчанию (`HH_TOKEN_AUTO_REFRESH=false`) сервис **не делает ни одного не-GET запроса**. Когда токен истекает, синхронизация завершается ошибкой 403 `token-expired`, и токены в `.env` нужно обновить вручную;
- при `HH_TOKEN_AUTO_REFRESH=true` и ответе `token-expired` выполняется один POST на `/token`, после чего запрос повторяется. Refresh token в hh.ru одноразовый, поэтому новая пара сохраняется в таблицу `hh_oauth_tokens` и дальше имеет приоритет над `.env`. Эти токены лежат в БД открытым текстом, доступ к БД нужно ограничить.

## TODO: что не удалось подтвердить по официальной документации

1. **amoCRM, список причин отказа.** В документации API v4 нет метода списка причин отказа (`GET /api/v4/leads/loss_reasons`): он встречается только как `_links.self.href` в примерах ответа. Причины отказа собираются из `_embedded.loss_reason` сделок (`with=loss_reason`, это задокументировано). Поэтому причины, которые не указаны ни в одной сделке, в таблицу не попадут. (`app/integrations/amo/client.py`)
2. **amoCRM, удалённые сущности.** Инкрементальная синхронизация не узнаёт об удалении сделок, задач и примечаний. Есть `with=only_deleted` для сделок, но он не реализован. Полная синхронизация удалённые записи тоже не помечает.
3. **amoCRM, лимит запросов.** Значение «7 запросов в секунду» на открытых страницах найти не удалось; описаны только коды 429/403 за превышение. Мы держим не больше 5 запросов в секунду, как требует ТЗ.
4. **amoCRM, notes.** Синхронизируются только примечания сделок (`/api/v4/leads/notes`). Примечания контактов и компаний не входили в задачу.
5. **hh.ru, `/me` → employer.id.** В спецификации объект `employer` в профиле менеджера (`MeManagerProfile`) помечен как deprecated, и его поля не описаны. Чтение `employer.id` оставлено запасным вариантом. Рекомендуется задать `HH_EMPLOYER_ID`. (`app/integrations/hh/client.py`)
6. **hh.ru, инкремент вакансий.** У списков вакансий нет задокументированного фильтра «изменено после» и поля `updated_at`, поэтому список вакансий всегда читается полностью (он дешёвый). В режиме `incremental` отклики берутся только по активным вакансиям, в режиме `full` — по активным и архивным.
7. **hh.ru, сортировка откликов.** Значения `order_by` для коллекций зависят от аккаунта (`collections[].order_types`) и в спецификации не перечислены. Гарантированной сортировки по `updated_at` нет, поэтому `incremental` листает все страницы откликов, но записывает только отклики с `updated_at ≥ курсор − overlap`. Экономится запись в БД, а не число запросов.
8. **hh.ru, параметры refresh.** Пример `grant_type=refresh_token` в спецификации содержит только `grant_type` и `refresh_token`. Мы дополнительно отправляем `client_id`/`client_secret`, если они заданы. (`app/integrations/hh/oauth.py`)
9. **hh.ru, лимит запросов.** Числовой лимит в спецификации не указан. Используем общие правила: не больше 5 запросов в секунду и повторы при 429/5xx.

## Таблицы

`amo_users`, `amo_pipelines`, `amo_statuses` (PK `(pipeline_id, id)`: системные статусы 142/143 повторяются в каждой воронке), `amo_loss_reasons`, `amo_leads`, `amo_tasks`, `amo_notes` (PK `(entity_type, id)`), `hh_vacancies`, `hh_negotiations`, `hh_candidates`, `hh_oauth_tokens`, `sync_state`.

Новая миграция после изменения моделей: `alembic revision --autogenerate -m "..."`.
