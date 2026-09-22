# DeepSeek API → opencode (агент)

Локальный **OpenAI-совместимый мост** к бесплатному веб-чату DeepSeek
([chat.deepseek.com](https://chat.deepseek.com)), который можно подключить к
**opencode** как обычного провайдера моделей и использовать как полноценного
агента (чтение/правка файлов, bash, поиск по коду и т.д.) — **без API-ключа и
оплаты**.

> **Оригинал проекта:** <https://github.com/sums001/Deepseek-API>
> Это неофициальный проект, не связанный с DeepSeek. Вы используете свой
> обычный аккаунт DeepSeek и несёте ответственность за соблюдение его условий.

---

## Содержание

- [Как это работает](#как-это-работает)
- [Требования](#требования)
- [Единый путь развёртки (копировать целиком)](#единый-путь-развёртки-копировать-целиком)
- [Пошагово, с пояснениями](#пошагово-с-пояснениями)
- [Проверка работоспособности](#проверка-работоспособности)
- [Интеграция с opencode](#интеграция-с-opencode)
- [Переключение моделей, DeepThink и веб-поиск](#переключение-моделей-deepthink-и-веб-поиск)
- [Переменные окружения](#переменные-окружения)
- [Ограничения и важные нюансы](#ограничения-и-важные-нюансы)
- [Обслуживание и troubleshooting](#обслуживание-и-troubleshooting)
- [Структура проекта](#структура-проекта)
- [Безопасность](#безопасность)
- [Лицензия](#лицензия)

---

## Как это работает

```
┌──────────────┐   OpenAI API    ┌──────────────────────┐   внутренний   ┌──────────────────┐
│   opencode   │ ───────────────▶│  server/api.py       │ ── протокол ──▶│  chat.deepseek   │
│ (агент/CLI)  │  localhost:8000 │  (FastAPI, /v1/...)  │  + PoW/WASM    │  .com (ваш аккаунт)│
└──────────────┘                 └──────────────────────┘                └──────────────────┘
                                        │
                                        ├── deepseek/auth.py   (вход через браузер, сессия в session/)
                                        └── deepseek/pow.py    (решение proof-of-work через wasmtime)
```

- **opencode** думает, что общается с обычным OpenAI-провайдером.
- **Мост** транслирует запросы в веб-чат DeepSeek, решает PoW-челлендж
  (`sha3_wasm_bg.wasm` в песочнице `wasmtime`) и отдаёт поток токенов обратно.
- Сессия входа сохраняется в `session/` (в git не попадает) и обновляется
  автоматически примерно раз в 5 часов.

---

## Требования

- **Python 3.9+** (рекомендуется 3.11/3.12)
- **Node.js 18+** — нужен только для opencode (не для моста)
- **Аккаунт DeepSeek** (бесплатный, тот же, что для chat.deepseek.com)
- **opencode** — <https://opencode.ai>
- ОС: Windows, macOS, Linux

---

## Единый путь развёртки (копировать целиком)

> Ниже — весь путь от нуля до работающего агента. Выполняйте блоки по порядку.
> Замените `~/projects` на удобную вам папку.

### macOS / Linux

```bash
# 0. Предпосылки: Python 3.9+, Node 18+, opencode
python3 --version && node --version && opencode --version

# 1. Клон проекта
mkdir -p ~/projects && cd ~/projects
git clone https://github.com/sums001/Deepseek-API.git
cd Deepseek-API

# 2. Виртуальное окружение
python3 -m venv venv
source venv/bin/activate

# 3. Зависимости + браузер для Playwright
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

# 4. Одноразовый вход в аккаунт DeepSeek (откроется окно браузера)
python -m deepseek.auth

# 5. Конфиг (значения по умолчанию подходят для локального запуска)
cp .env.example .env

# 6. Запуск сервера (оставьте терминал открытым)
python app.py
# -> http://127.0.0.1:8000
```

Проверка (во **втором** терминале):

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/v1/models
```

### Windows (PowerShell)

```powershell
python --version; node --version; opencode --version

mkdir $HOME\projects; cd $HOME\projects
git clone https://github.com/sums001/Deepseek-API.git
cd Deepseek-API

python -m venv venv
.\venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

python -m deepseek.auth
Copy-Item .env.example .env
python app.py
```

> Если PowerShell блокирует активацию venv:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
> (или активируйте через `venv\Scripts\activate.bat` в `cmd.exe`).

### Регистрация провайдера и агента в opencode

Содержимое файлов `opencode.json` и `.opencode/agent/*.md` — в разделе
[Интеграция с opencode](#интеграция-с-opencode). После их создания
**перезапустите opencode**.

---

## Пошагово, с пояснениями

### 1. Клон

```bash
git clone https://github.com/sums001/Deepseek-API.git
cd Deepseek-API
```

### 2. Виртуальное окружение

Изолирует зависимости проекта от системного Python.

```bash
python3 -m venv venv
source venv/bin/activate        # macOS/Linux
# .\venv\Scripts\Activate.ps1   # Windows PowerShell
```

### 3. Зависимости

```bash
pip install -r requirements.txt
playwright install chromium     # разовая установка браузера
```

`requirements.txt` тянет: `playwright` (вход + PoW), `httpx`, `fastapi`,
`uvicorn`, `pydantic`, `python-dotenv`, `wasmtime`, `openai`.

### 4. Вход в DeepSeek (один раз)

```bash
python -m deepseek.auth
```

Откроется настоящий браузер — войдите в аккаунт и пройдите капчу
(human-check). После этого токен и cookies сохранятся в `session/` и будут
переиспользоваться. Сессия обновляется автоматически; повторный вход нужен,
только если она полностью истекла.

### 5. Конфигурация `.env`

```bash
cp .env.example .env
```

Для локального запуска значения по умолчанию подходят. Пароли в `.env` не
хранятся — вход выполняется вручную в браузере.

### 6. Запуск сервера

```bash
python app.py
# -> DeepSeek OpenAI-compatible API on http://127.0.0.1:8000
```

Альтернатива через uvicorn с другим адресом:

```bash
HOST=0.0.0.0 PORT=8080 python app.py
# или
uvicorn server.api:app --host 0.0.0.0 --port 8080
```

---

## Проверка работоспособности

```bash
# 1) Живость
curl http://127.0.0.1:8000/healthz
# {"status":"ok"}

# 2) Список моделей
curl http://127.0.0.1:8000/v1/models

# 3) Простой чат
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"Привет!"}]}'
```

Python SDK:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")
r = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(r.choices[0].message.content)
```

Готовые примеры: [examples/](examples/) (`01_*` — напрямую из Python, `04_*`–`06_*` — через сервер).

---

## Интеграция с opencode

opencode подключается к мосту как к **OpenAI-совместимому провайдеру**.

### 1. Провайдер в `opencode.json`

Создайте `opencode.json` в корне вашего рабочего проекта (или в
`~/.config/opencode/opencode.json` для глобальной настройки):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "deepseek-bridge": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "DeepSeek (local bridge)",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "unused"
      },
      "models": {
        "deepseek-chat": {
          "name": "DeepSeek Chat (Instant)",
          "tool_call": true,
          "reasoning": false,
          "limit": { "context": 64000, "output": 8192 }
        },
        "deepseek-expert": {
          "name": "DeepSeek Expert",
          "tool_call": true,
          "reasoning": false,
          "limit": { "context": 64000, "output": 8192 }
        }
      }
    }
  }
}
```

- `npm: "@ai-sdk/openai-compatible"` — универсальный OpenAI-совместимый драйвер.
- `apiKey` обязателен для SDK, но мост его игнорирует.
- `model` в opencode указывается как `deepseek-bridge/deepseek-chat` или
  `deepseek-bridge/deepseek-expert`.
- `limit.context` — приблизительный; уменьшите, если получаете ошибки о
  переполнении контекста.

### 2. (Опционально) Сделать DeepSeek моделью по умолчанию

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "deepseek-bridge/deepseek-chat",
  "small_model": "deepseek-bridge/deepseek-chat",
  "provider": { "deepseek-bridge": { "npm": "@ai-sdk/openai-compatible", "name": "DeepSeek (local bridge)", "options": { "baseURL": "http://127.0.0.1:8000/v1", "apiKey": "unused" }, "models": { "deepseek-chat": { "name": "DeepSeek Chat (Instant)", "tool_call": true }, "deepseek-expert": { "name": "DeepSeek Expert", "tool_call": true } } } }
}
```

> `small_model` используется для служебных задач (генерация заголовков и т.п.).

### 3. Агент

Файл `.opencode/agent/deepseek.md` в корне рабочего проекта:

```markdown
---
description: Инженерный агент на DeepSeek через локальный мост
mode: primary
model: deepseek-bridge/deepseek-expert
temperature: 0.3
permission:
  edit: allow
  webfetch: allow
  bash:
    "git *": allow
    "*": ask
---

Ты — аккуратный инженерный агент. Сначала изучаешь код, затем предлагаешь
минимальные точечные правки. Не добавляешь комментарии без просьбы. После
изменений запускаешь линтер и тесты проекта, если они есть.
```

- `mode: primary` — агент доступен как основной (переключается в TUI/через
  `default_agent`). Для вспомогательного агента используйте `mode: subagent`.
- Тело файла — это системный промпт агента.

Чтобы назначить его агентом по умолчанию, добавьте в `opencode.json`:

```json
{ "default_agent": "deepseek" }
```

### 4. Перезапуск

opencode читает конфиг один раз при старте. **Полностью закройте и заново
запустите opencode** после создания/правки `opencode.json` и файлов агентов.

Проверка: в opencode откройте выбор модели — должны появиться
`DeepSeek Chat (Instant)` и `DeepSeek Expert` под провайдером
`DeepSeek (local bridge)`.

---

## Переключение моделей, DeepThink и веб-поиск

| Что | Значение в opencode | Примечание |
| --- | --- | --- |
| Быстрая модель | `deepseek-bridge/deepseek-chat` | По умолчанию |
| Экспертная модель | `deepseek-bridge/deepseek-expert` | Сильнее, медленнее |
| DeepThink (reasoning) | — | Через `extra_body`, см. ниже |
| Веб-поиск | — | Через `extra_body`, см. ниже |

Модель задаётся в opencode штатно (выбор модели или поле `model` в агенте).
`thinking` (DeepThink) и `search` (веб-поиск) — это **не** модели, а
дополнительные флаги тела запроса, которые opencode напрямую не передаёт.
Их можно включить плагином (см. ниже) либо запросами через `curl`/SDK:

```python
resp = client.chat.completions.create(
    model="deepseek-expert",
    messages=[{"role": "user", "content": "Что нового в мире?"}],
    extra_body={"thinking": True, "search": True},
)
```

### Плагин для включения DeepThink в opencode (опционально, экспериментально)

`.opencode/plugin/deepseek-thinking.js` в рабочем проекте:

```js
export const DeepSeekThinking = async () => ({
  "chat.params": async (input, output) => {
    if (input.model?.providerID === "deepseek-bridge") {
      output.thinking = true;
      // output.search = true;
    }
  },
});
```

> Хук `chat.params` позволяет дополнить параметры запроса к провайдеру.
> Поведение нестандартных полей зависит от версии opencode/SDK — проверьте на
> своей сборке. Если флаги не доходят, используйте `curl`/SDK напрямую.

---

## Переменные окружения

Файл `.env` (копия `.env.example`):

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Адрес прослушивания |
| `PORT` | `8000` | Порт |
| `RATE_LIMIT_PER_MINUTE` | `30` | Лимит запросов/мин на IP клиента (`/healthz` не считается) |
| `DEEPSEEK_PROFILE_DIR` | — | Переиспользовать существующий профиль Chrome с активной сессией |
| `SERVER_INTERACTIVE_LOGIN` | `1` | Открывать окно браузера при отсутствии сессии; `0` — отдавать `503` (для headless) |
| `SESSION_REFRESH_ENABLED` | `1` | Фоновое обновление сессии |
| `SESSION_REFRESH_INTERVAL` | `18000` (5 ч) | Интервал обновления, сек (меньше `SESSION_MAX_AGE` = 6 ч) |
| `REFRESH_BROWSER_CHANNEL` | `chromium-headless-shell` | Канал Playwright для headless-обновления (меньше RAM) |
| `REFRESH_BROWSER_CHANNEL_FALLBACK` | `chrome` | Запасной канал, если headless не читает cookies; пусто — отключить |

Пример:

```bash
HOST=0.0.0.0 PORT=8080 RATE_LIMIT_PER_MINUTE=60 python app.py
```

---

## Ограничения и важные нюансы

- **Сериализация запросов.** Один общий аккаунт, PoW-хранилище `wasmtime`
  не реентерабельно, поэтому апстрим-вызовы выполняются **по одному**
  (см. `server/api.py`). Параллельные запросы встают в очередь — не стоит
  запускать несколько агентов/сессий одновременно.
- **Tool calling эмулируется.** Мост буферизует ответ и парсит его в tool
  calls (`server/api.py`, `parse_tool_calls`). Работает, но менее надёжно, чем
  у нативных API: возможны сбои на сложных многошаговых цепочках.
- **Нет реального подсчёта токенов.** `usage` — грубая оценка ~4 символа/токен.
- **Большинство OpenAI-параметров игнорируется** (`temperature`, `top_p`,
  `max_tokens` и т.д.). Действуют только `model`, `messages`, `stream`,
  `conversation_id`, `thinking`, `search`.
- **Vision не поддерживается** (нет загрузки изображений).
- **Идентификатор диалога.** `conversation_id` фиксирует модель при создании
  потока; при продолжении `model` игнорируется.
- **Лимит рейта.** При `429` используйте экспоненциальную задержку —
  официальный `openai` SDK делает это автоматически.
- **Не хаммерьте аккаунт.** Это ваш обычный аккаунт DeepSeek; массовые
  автоматические запросы могут привести к блокировке.

---

## Обслуживание и troubleshooting

**Сессия истекла / `503 login_required`**

```bash
python -m deepseek.auth   # войдите заново
```

Убедитесь, что `SERVER_INTERACTIVE_LOGIN=0` на headless-деплое, а окно входа
выполнено заранее.

**`Playwright Sync API inside the asyncio loop`**

Не вызывайте синхронный Playwright из event loop — мост уже оборачивает вызовы
в `run_in_threadpool`. Такое сообщение обычно означает нестандартную
модификацию кода.

**Ошибки PoW / wasmtime**

```bash
pip install --upgrade wasmtime
```

**Пустая headless-сессия (macOS, Chrome-профиль)**

По умолчанию фолбэк на `chrome`. Оставьте `REFRESH_BROWSER_CHANNEL_FALLBACK=chrome`
или задайте `DEEPSEEK_PROFILE_DIR`.

**`429 Too Many Requests`**

Поднимите `RATE_LIMIT_PER_MINUTE` и/или добавьте ретраи с backoff.

**Порт занят**

```bash
PORT=8080 python app.py
```

и укажите `http://127.0.0.1:8080/v1` в `baseURL` провайдера opencode.

**Изменения в `opencode.json`/агентах не применились**

Перезапустите opencode — конфиг не перезагружается на лету.

---

## Структура проекта

| Путь | Назначение |
| --- | --- |
| `app.py` | Точка входа — запускает сервер |
| `deepseek/` | Ядро: `DeepSeekClient`, вход (`auth.py`), HTTP-драйвер (`client.py`), PoW (`pow.py`) |
| `server/` | FastAPI OpenAI-совместимый сервер (`api.py`, `config.py`, `openai_format.py`, `ratelimit.py`, `schemas.py`) |
| `examples/` | Запускаемые примеры (прямой Python и через сервер) |
| `session/` | Сохранённая сессия (cookies + токен), **git-ignored** |
| `.env.example` | Шаблон конфигурации |
| `requirements.txt` | Python-зависимости |

---

## Безопасность

- Всё в `session/` (cookies + bearer-токен) остаётся **на вашей машине** и
  исключено из git (`.gitignore`). Никогда не коммитьте `session/`.
- Пароли/секреты в `.env` не хранятся — вход выполняется вручную в браузере.
- При `HOST=0.0.0.0` мост доступен в сети без аутентификации. Биндитесь только
  на `127.0.0.1` или закрывайте firewall'ом/прокси.
- Не публикуйте `session/` и `.env` в публичных репозиториях.

---

## Лицензия

[MIT License](LICENSE). Проект неофициальный; вы отвечаете за соблюдение
условий использования DeepSeek.

**Оригинал:** <https://github.com/sums001/Deepseek-API>
