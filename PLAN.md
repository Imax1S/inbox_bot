# План миграции Inbox Bot на AWS Lambda

## Финальная архитектура

```
Telegram
  │
  ▼ (webhook POST)
API Gateway (HTTP API)
  │
  ▼
Lambda: webhook_handler         ← обрабатывает все входящие сообщения/команды
  │                                (timeout: 30s, memory: 256MB)
  │── save_item → DynamoDB
  │── /generate → запускает Step Functions
  │── /items, /status, ... → читает DynamoDB
  │── /setup state → DynamoDB (conversation state)
  │
  ▼ (StartExecution)
Step Functions: DigestPipeline
  │
  ├─► Lambda: step_filter       (timeout: 5 min, memory: 512MB)
  ├─► Lambda: step_cluster      (timeout: 5 min)
  ├─► Lambda: step_research     (timeout: 10 min) ← может быть N кластеров
  ├─► Lambda: step_write        (timeout: 10 min) ← может быть N кластеров
  ├─► Lambda: step_edit         (timeout: 5 min)
  ├─► Lambda: step_translate    (timeout: 5 min, conditional)
  └─► Lambda: step_finalize     ← отправляет файл в Telegram + S3
        │
        ▼
      DynamoDB (pipeline_runs, step_logs)
      S3 (digest files)
      Telegram (send document)
```

## Стоимость: $0/мес

| Сервис | Free Tier | Наш расход |
|--------|-----------|------------|
| Lambda | 1M req + 400K GB-s/мес | ~100 req/мес |
| API Gateway | 1M req/мес | ~100 req/мес |
| DynamoDB | 25 GB + 25 RCU/WCU | <1 MB, ~50 ops/мес |
| Step Functions | 4000 transitions/мес | ~40/мес |
| S3 | 5 GB + 20K GET + 2K PUT | ~4 files/мес |

Free Tier действует 12 месяцев. После — всё равно <$1/мес при таком объёме.

---

## Фаза 0: Подготовка AWS-окружения

### 0.1 — AWS Account + CLI
- Создать AWS аккаунт (если нет)
- Установить AWS CLI v2: `brew install awscli` / `apt install awscli`
- Настроить: `aws configure` (Access Key ID, Secret, region = `eu-central-1`)
- Установить AWS SAM CLI: `brew install aws-sam-cli`

### 0.2 — Структура проекта
Добавить директорию `infra/` для AWS-ресурсов:
```
infra/
├── template.yaml          # SAM template (Lambda + API GW + DynamoDB + Step Functions + S3)
└── samconfig.toml         # SAM deploy config
```

---

## Фаза 1: DynamoDB вместо SQLite

**Файлы:** `src/db/database.py`, `requirements.txt`

### Дизайн таблиц DynamoDB

**Одна таблица `InboxBot`** (single-table design):

| PK | SK | Данные |
|----|-----|--------|
| `ITEM#{id}` | `ITEM#{id}` | все поля Item |
| `ITEM#{id}` | `WEEK#{week_id}` | GSI-запись для выборки по неделе |
| `RUN#{id}` | `RUN#{id}` | все поля PipelineRun |
| `RUN#{id}` | `STEP#{step_id}` | все поля StepLog |
| `SETTING` | `{key}` | value |

**GSI-1** (week_id index): `GSI1PK = WEEK#{week_id}`, `GSI1SK = created_at`
— для `get_items_by_week()`

**GSI-2** (runs index): `GSI1PK = RUNS`, `GSI1SK = started_at`
— для `get_last_run()`, `get_recent_runs()`

### Что делать
1. Добавить `boto3` в `requirements.txt`
2. Создать `src/db/dynamodb.py` — новая реализация `Database` с тем же публичным интерфейсом
3. Все 15 методов Database сохраняют сигнатуры, меняется только внутренняя реализация
4. В `src/db/__init__.py` — переключение между SQLite и DynamoDB через env var
5. `models.py` — **без изменений**

### Публичный интерфейс (сохранить как есть)
```python
class Database:
    async def init(self) -> None
    async def save_item(self, item: Item) -> None
    async def get_items_by_week(self, week_id, status) -> list[Item]
    async def get_item(self, item_id: str) -> Item | None
    async def find_item_by_short_id(self, short_id: str) -> Item | None
    async def delete_item(self, item_id: str) -> bool
    async def update_items_status(self, item_ids, status) -> None
    async def count_items_by_week(self, week_id) -> int
    async def save_pipeline_run(self, run: PipelineRun) -> None
    async def update_pipeline_run(self, run_id, status, ...) -> None
    async def get_last_run(self, week_id) -> PipelineRun | None
    async def get_recent_runs(self, limit) -> list[PipelineRun]
    async def save_step_log(self, step: StepLog) -> None
    async def get_setting(self, key, default) -> str | None
    async def set_setting(self, key, value) -> None
    @staticmethod
    def current_week_id() -> str
```

### Тесты
- Адаптировать существующие тесты для DynamoDB (мокать boto3)
- Или использовать `moto` для local DynamoDB mock

---

## Фаза 2: Webhook Lambda вместо Polling

**Файлы:** `src/telegram/bot.py`, `src/main.py`, новый `src/lambda_handlers/webhook.py`

### Что меняется

Сейчас: `app.run_polling()` — long-running процесс.
Нужно: Lambda получает POST от Telegram webhook, обрабатывает Update, возвращает 200.

### Подход

1. Создать `src/lambda_handlers/webhook.py`:
```python
# Lambda handler для Telegram webhook
async def handler(event, context):
    body = json.loads(event["body"])
    update = Update.de_json(body, bot)
    await app.process_update(update)
    return {"statusCode": 200}
```

2. python-telegram-bot поддерживает обработку отдельных Update через `app.process_update()`.
   Нужно инициализировать Application один раз при cold start (глобальная переменная).

3. **ConversationHandler state** — не будет работать в Lambda (in-memory).
   Решение: сохранять состояние `/setup` в DynamoDB (таблица settings с ключом `conversation_state_{user_id}`).
   Простая реализация: перед обработкой update — загрузить state; после — сохранить.

4. **`_generating` flag** — заменить на запись в DynamoDB (`pipeline_lock`).
   Перед запуском Step Functions — проверить/установить lock. Финализация снимает lock.

5. **Telegram webhook setup**: при деплое — вызвать Telegram API:
   ```
   POST https://api.telegram.org/bot<TOKEN>/setWebhook
   url=https://<API_GW_URL>/webhook
   ```
   Добавить в SAM template как Custom Resource или в post-deploy скрипт.

### Obsidian Writer → S3 + Telegram
- Дайджест сохраняется в S3 bucket
- Файл отправляется пользователю через Telegram (`send_document`)
- Obsidian-интеграция опциональна (можно настроить sync S3 → Obsidian позже)

---

## Фаза 3: Step Functions для Pipeline

**Файлы:** новый `src/lambda_handlers/pipeline_steps.py`, `infra/template.yaml`

### State Machine Definition

```yaml
# В SAM template (ASL в YAML)
DigestPipeline:
  Type: AWS::Serverless::StateMachine
  Properties:
    Definition:
      StartAt: Filter
      States:
        Filter:
          Type: Task
          Resource: !GetAtt FilterFunction.Arn
          Next: Cluster
        Cluster:
          Type: Task
          Resource: !GetAtt ClusterFunction.Arn
          Next: Research
        Research:
          Type: Task
          Resource: !GetAtt ResearchFunction.Arn
          Next: Write
        Write:
          Type: Task
          Resource: !GetAtt WriteFunction.Arn
          Next: Edit
        Edit:
          Type: Task
          Resource: !GetAtt EditFunction.Arn
          Next: ShouldTranslate
        ShouldTranslate:
          Type: Choice
          Choices:
            - Variable: "$.needs_translation"
              BooleanEquals: true
              Next: Translate
          Default: Finalize
        Translate:
          Type: Task
          Resource: !GetAtt TranslateFunction.Arn
          Next: Finalize
        Finalize:
          Type: Task
          Resource: !GetAtt FinalizeFunction.Arn
          End: true
```

### Передача данных между шагами

Каждый шаг Lambda принимает и возвращает JSON:

```python
# Вход Filter:
{
    "run_id": "...",
    "week_id": "2026-W09",
    "chat_id": 123456,     # для Telegram уведомлений
    "item_ids": ["id1", "id2", ...],
}

# Выход Filter → вход Cluster:
{
    "run_id": "...",
    "week_id": "2026-W09",
    "chat_id": 123456,
    "item_ids": ["id1", "id3", ...],  # отфильтрованные убраны
    "filter_report": [...]
}

# Выход Cluster → вход Research:
{
    ...
    "clusters": [...],           # сериализованные Cluster объекты
    "quick_bites_item_ids": [...]
}

# И т.д. — каждый шаг дополняет state
```

### Lambda для каждого шага
Создать `src/lambda_handlers/pipeline_steps.py` с функциями:
- `filter_handler(event, context)`
- `cluster_handler(event, context)`
- `research_handler(event, context)`
- `write_handler(event, context)`
- `edit_handler(event, context)`
- `translate_handler(event, context)`
- `finalize_handler(event, context)`

Каждый handler:
1. Читает items/data из DynamoDB по IDs из event
2. Создаёт нужного агента
3. Вызывает `agent.process(...)`
4. Сохраняет результат (step_log в DynamoDB)
5. Возвращает обновлённый state для следующего шага

### Status Updates
`finalize_handler` отправляет итоговый файл в Telegram.
Промежуточные status updates — опционально (каждый шаг может слать update через Bot API).

---

## Фаза 4: SAM Template (Infrastructure as Code)

**Файл:** `infra/template.yaml`

### Ресурсы
```yaml
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31

Globals:
  Function:
    Runtime: python3.12
    Handler: handler
    Timeout: 30
    MemorySize: 256
    Environment:
      Variables:
        DYNAMODB_TABLE: !Ref InboxBotTable
        S3_BUCKET: !Ref DigestBucket

Resources:
  # ── API Gateway ──
  WebhookApi:
    Type: AWS::Serverless::HttpApi

  # ── DynamoDB ──
  InboxBotTable:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: PAY_PER_REQUEST    # $0 при малой нагрузке
      AttributeDefinitions: [...]
      KeySchema: [...]
      GlobalSecondaryIndexes: [...]

  # ── S3 ──
  DigestBucket:
    Type: AWS::S3::Bucket

  # ── Lambda Functions ──
  WebhookFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: ../
      Handler: src.lambda_handlers.webhook.handler
      Timeout: 30
      Events:
        Webhook:
          Type: HttpApi
          Properties:
            ApiId: !Ref WebhookApi
            Path: /webhook
            Method: POST

  FilterFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: ../
      Handler: src.lambda_handlers.pipeline_steps.filter_handler
      Timeout: 300     # 5 min
      MemorySize: 512

  # ... аналогично для cluster, research, write, edit, translate, finalize

  # ── Step Functions ──
  DigestPipeline:
    Type: AWS::Serverless::StateMachine
    Properties:
      DefinitionUri: statemachine.asl.json
      Policies:
        - LambdaInvokePolicy:
            FunctionName: !Ref FilterFunction
        # ... остальные функции

  # ── Secrets (для API keys) ──
  # Храним в SSM Parameter Store (бесплатно) или Secrets Manager
```

### Секреты
Хранить в **SSM Parameter Store** (бесплатный):
```bash
aws ssm put-parameter --name /inbox-bot/TELEGRAM_BOT_TOKEN --value "..." --type SecureString
aws ssm put-parameter --name /inbox-bot/ANTHROPIC_API_KEY --value "..." --type SecureString
aws ssm put-parameter --name /inbox-bot/TELEGRAM_USER_ID --value "..." --type String
```

Lambda читает при cold start через boto3 SSM client.

---

## Фаза 5: CI/CD — Deploy после merge

**Файл:** `.github/workflows/deploy.yml`

```yaml
name: Deploy to AWS

on:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -r requirements.txt
      - run: pytest tests/

  deploy:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - uses: aws-actions/setup-sam@v2
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: eu-central-1
      - run: sam build --template-file infra/template.yaml
      - run: sam deploy --no-confirm-changeset --no-fail-on-empty-changeset
      # Set Telegram webhook after deploy
      - run: |
          API_URL=$(aws cloudformation describe-stacks \
            --stack-name inbox-bot \
            --query 'Stacks[0].Outputs[?OutputKey==`WebhookApiUrl`].OutputValue' \
            --output text)
          curl -s "https://api.telegram.org/bot${{ secrets.TELEGRAM_BOT_TOKEN }}/setWebhook?url=${API_URL}/webhook"
```

### GitHub Secrets для CI/CD
```
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
TELEGRAM_BOT_TOKEN    # для setWebhook после деплоя
```

Создать IAM user `inbox-bot-deploy` с policy:
- `AWSLambdaFullAccess`
- `AmazonDynamoDBFullAccess`
- `AmazonS3FullAccess`
- `AWSStepFunctionsFullAccess`
- `AmazonAPIGatewayAdministrator`
- `AWSCloudFormationFullAccess`
- `IAMFullAccess` (для создания ролей)

---

## Порядок реализации

| # | Задача | Изменяемые файлы | Сложность |
|---|--------|-------------------|-----------|
| 1 | SAM template (базовый: DynamoDB + S3 + webhook Lambda) | `infra/template.yaml`, `infra/samconfig.toml` | Средняя |
| 2 | DynamoDB Database class | `src/db/dynamodb.py`, `requirements.txt` | Средняя |
| 3 | Webhook Lambda handler | `src/lambda_handlers/webhook.py` | Средняя |
| 4 | Адаптация bot.py для webhook mode | `src/telegram/bot.py` | Средняя |
| 5 | Pipeline step handlers | `src/lambda_handlers/pipeline_steps.py` | Высокая |
| 6 | Step Functions definition | `infra/template.yaml` (state machine) | Средняя |
| 7 | S3 writer вместо obsidian_writer | `src/obsidian_writer.py` или новый `src/s3_writer.py` | Лёгкая |
| 8 | SSM для секретов + config.py адаптация | `src/config.py` | Лёгкая |
| 9 | GitHub Actions CI/CD | `.github/workflows/deploy.yml` | Лёгкая |
| 10 | Первый деплой + тестирование | — | — |

### Что НЕ меняется
- `src/agents/*` — все агенты
- `src/db/models.py` — dataclass'ы
- `src/llm/provider.py` — LLM провайдеры
- `src/content/*` — классификатор и URL парсер
- `prompts/*` — все промпты
- `user_profile.json`
- `tests/` — только адаптация mock'ов для DynamoDB

### Что остаётся рабочим (Docker mode)
Оригинальный `src/main.py` + `src/db/database.py` (SQLite) сохраняются.
DynamoDB реализация — отдельный файл. Переключение через env var `DB_BACKEND=dynamodb|sqlite`.
Можно продолжать запускать через Docker локально.
