# Подробная настройка

[Главная](../README.md) · [English](SETUP.en.md)

Этот справочник сохраняет подробности установки, API, агентов и обслуживания. Статус провайдеров и демонстрации — на главной. Браузерные GLM/Kimi и экспериментальные адаптеры настраиваются по [отдельной инструкции](../browser/README.md).

Ниже сохранён подробный путь для DeepSeek. Для первого запуска с проверенным
сейчас Qwen используйте [быстрый старт](../README.md#быстрый-старт). На 2 октября
2026 наш аккаунт DeepSeek возвращает `user is muted`; повторный вход не помог.

Все провайдеры теперь выключены по умолчанию. **Инструкция ниже описывает
осознанное включение DeepSeek (`DEEPSEEK_ENABLED=1` в `.env`) с риском ограничения
аккаунта.** Не выполняйте её на заблокированном аккаунте. Для первого запуска
используйте Qwen из главной инструкции. Отказы сохраняют паузу в
`session/provider-pauses.json`; сервер не обновляет сессии и не повторяет запросы.
Проверка состояния: `python -m providers.access status`. После ручной проверки
доступа на обычном сайте: `python -m providers.access resume qwen` (укажите
нужного провайдера). Перелогин паузу не снимает. По умолчанию между попытками
одного провайдера — 10 секунд, включая продолжения инструментов.

## Требования

- **Python 3.9+** (рекомендуется 3.11/3.12)
- **OpenCode** — установите по [официальной инструкции](https://opencode.ai/docs/). Node.js нужен только при установке через npm.
- **Аккаунт DeepSeek** (бесплатный, тот же, что для chat.deepseek.com)
- ОС: Windows, macOS, Linux

---

## Единый путь развёртки (копировать целиком)

> Ниже — весь путь от нуля до работающего агента. Выполняйте блоки по порядку.
> Замените `~/projects` на удобную вам папку.

### macOS / Linux

```bash
# 0. Предпосылки: Python 3.9+, opencode
python3 --version && opencode --version

# 1. Клон проекта
mkdir -p ~/projects && cd ~/projects
git clone https://github.com/Tsuev/opencode-deepseek.git
cd opencode-deepseek

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
# Explicit optional DeepSeek path; see account restriction warning above.
python -c "from pathlib import Path; p=Path('.env'); p.write_text(p.read_text().replace('DEEPSEEK_ENABLED=0','DEEPSEEK_ENABLED=1'))"

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
python --version; opencode --version

mkdir $HOME\projects; cd $HOME\projects
git clone https://github.com/Tsuev/opencode-deepseek.git
cd opencode-deepseek

python -m venv venv
.\venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

python -m deepseek.auth
Copy-Item .env.example .env
(Get-Content .env) -replace 'DEEPSEEK_ENABLED=0', 'DEEPSEEK_ENABLED=1' | Set-Content .env
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
git clone https://github.com/Tsuev/opencode-deepseek.git
cd opencode-deepseek
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
переиспользоваться. Сессия не обновляется автоматически; при истечении срока войдите вручную.

### 5. Конфигурация `.env`

```bash
cp .env.example .env
# Explicit optional DeepSeek path; see account restriction warning above.
python -c "from pathlib import Path; p=Path('.env'); p.write_text(p.read_text().replace('DEEPSEEK_ENABLED=0','DEEPSEEK_ENABLED=1'))"
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
uvicorn server.api:app --host 0.0.0.0 --port 8080 --no-proxy-headers
```

### 7. Автозапуск при входе в систему (macOS, опционально)

Чтобы сервер поднимался сам и перезапускался при падении, заведите LaunchAgent
`~/Library/LaunchAgents/com.deepseek.api.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.deepseek.api</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/ВЫ/projects/opencode-deepseek/venv/bin/python</string>
        <string>app.py</string>
    </array>
    <key>WorkingDirectory</key><string>/Users/ВЫ/projects/opencode-deepseek</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOST</key><string>127.0.0.1</string>
        <key>PORT</key><string>8000</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/Users/ВЫ/projects/opencode-deepseek/logs/server.log</string>
    <key>StandardErrorPath</key><string>/Users/ВЫ/projects/opencode-deepseek/logs/server.err</string>
</dict>
</plist>
```

```bash
mkdir -p logs
launchctl load  ~/Library/LaunchAgents/com.deepseek.api.plist   # включить
launchctl kickstart -k "gui/$(id -u)/com.deepseek.api"          # перезапуск после правок кода
launchctl unload ~/Library/LaunchAgents/com.deepseek.api.plist  # выключить
```

> Важно: при `reload=False` (как в `app.py`) изменения кода подхватываются
> только после перезапуска — используйте `kickstart -k`. Логи смотрите в
> `logs/server.log` и `logs/server.err`.

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

Готовые примеры: [examples/](../examples/) (`01_*` — напрямую из Python, `04_*`–`06_*` — через сервер).

---

## Куда слать запросы: URL и API-ключ

Базовый адрес — `http://localhost:8000/v1` (или `http://127.0.0.1:8000/v1`).
Порт меняется переменной `PORT` в `.env` / launchd.

| Метод | URL | Назначение |
| --- | --- | --- |
| `POST` | `http://localhost:8000/v1/chat/completions` | Чат; стриминг через `"stream": true` |
| `GET` | `http://localhost:8000/v1/models` | Список моделей (`deepseek-chat`, `deepseek-expert`) |
| `GET` | `http://localhost:8000/healthz` | Проверка живости (без рейт-лимита) |

**API-ключ:** любой. Мост **не проверяет** `Authorization` и не читает его
(`server/api.py` не смотрит заголовок), поэтому в SDK, где ключ обязателен
синтаксически, пишут заглушку — исторически `api_key="unused"`. В чистом HTTP
через `curl` заголовок `Authorization` вообще не нужен.

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")
```

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"Hi"}]}'
```

Нестандартные поля (вне схемы OpenAI) передаются через `extra_body`:
`thinking` (DeepThink), `search` (веб-поиск), `conversation_id` (продолжить
диалог; при resume модель игнорируется). Лимит — `RATE_LIMIT_PER_MINUTE`
(по умолчанию 30/мин на IP), `/healthz` не считается.

---

## Чат в терминале

`chat.py` — интерактивный REPL, который ходит в chat.deepseek.com напрямую
через `DeepSeekClient`. Сервер (`app.py`) для этого не нужен.

```bash
./ds                      # продолжить последний диалог
./ds --new                # начать новый диалог
./ds --model expert       # Expert — сильнее и медленнее, чем Instant
./ds --think --search     # DeepThink-размышление + веб-поиск
./ds --no-think           # выключить DeepThink для этого запуска
```

Внутри чата:

| Команда | Что делает |
| --- | --- |
| `/new` | новый диалог (история на сайте сохраняется) |
| `/model default` \| `/model expert` | сменить модель (модель фиксируется при создании треда, поэтому смена открывает новый диалог) |
| `/think` | переключить DeepThink |
| `/search` | переключить веб-поиск |
| `/status` | текущая модель и переключатели |
| `/exit` | выйти (работает и Ctrl-D) |

Ответ печатается по мере генерации. Строка, заканчивающаяся на `\`, продолжается
на следующей. Текущий диалог и настройки сохраняются в `session/chat_state.json`,
так что следующий запуск `./ds` его продолжит.

Запуск из любой директории — поставьте shim один раз:

```bash
mkdir -p ~/bin && ln -sf "$(pwd)/bin/ds-chat" ~/bin/ds-chat
# убедитесь, что ~/bin в PATH
```

Вариант `Command+L` прямо в терминале Kaku — в
**[docs/TERMINAL_CHAT.md](../docs/TERMINAL_CHAT.md)**.

Демонстрация `Command+L` в Kaku (25 секунд):

[![Command+L в Kaku — чат DeepSeek](../docs/media/kaku-ai.png)](docs/media/kaku-ai.mp4)

Видео: [`docs/media/kaku-ai.mp4`](../docs/media/kaku-ai.mp4) — H.264/AAC,
1276×992, 1.4 МБ. Превью: `docs/media/kaku-ai.png` (нажмите на картинку, чтобы
открыть видео).

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
    "local-deepseek": {
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
- `model` в opencode указывается как `local-deepseek/deepseek-chat` или
  `local-deepseek/deepseek-expert`.
- `limit.context` — приблизительный; уменьшите, если получаете ошибки о
  переполнении контекста.

### 2. Основная и служебная модели

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "local-qwen/qwen3.8-omni-flash",
  "small_model": "local-qwen/qwen3.8-omni-flash",
  "enabled_providers": [
    "local-qwen"
  ],
  "provider": {
    "local-qwen": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Qwen (local bridge)",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "unused"
      },
      "models": {
        "qwen3.8-omni-flash": {
          "name": "Qwen3.8 Omni Flash",
          "tool_call": true
        }
      }
    }
  }
}
```

Включите `QWEN_ENABLED=1` и выполните вход Qwen. `small_model` тоже отправляет
запросы (например, для заголовков). Не назначайте ему DeepSeek скрыто.

### 3. Агент

Файл `.opencode/agent/deepseek.md` в корне рабочего проекта:

```markdown
---
description: Инженерный агент на DeepSeek через локальный мост
mode: primary
model: local-deepseek/deepseek-expert
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

### 5. Почему модель не вносит правки (эмуляция tool calling)

У веб-чата DeepSeek **нет канала function calling**, поэтому мост эмулирует его
текстом: подмешивает в промпт инструкцию «ответь ровно одним блоком кода
`tool_calls` с JSON-массивом» и затем парсит ответ обратно
([server/openai_format.py](../server/openai_format.py)). Если модель пишет прозу
вместо вызова, opencode просто печатает текст и **ничего не выполняет**.

Что сделано для надёжности:

- только один полный блок `tool_calls`, занимающий весь ответ, превращается
  в вызовы; JSON в прозе, XML/DSML и псевдовызовы остаются обычным текстом;
- весь массив проверяется до выдачи: неизвестный инструмент, невалидные
  аргументы или нарушение `tool_choice` дают `502 invalid_tool_response`;
- обрезанный блок продолжается в том же диалоге; частичные вызовы не выдаются;
- инструкция усилена (запрет прозы + пример + напоминание в конце);
- плагин дисциплины добавляет позднюю системную инструкцию.

Диагностика: запустите сервер с `DEBUG_TOOLCALLS=1` — при нераспознанном вызове
в лог (`logs/server.log` или stdout) попадёт сырой ответ модели.

```bash
DEBUG_TOOLCALLS=1 python app.py
```

Проверить мост напрямую (без opencode):

```bash
curl http://127.0.0.1:8000/v1/chat/completions -H "Content-Type: application/json" -d '{
  "model": "deepseek-chat",
  "messages": [{"role":"user","content":"Create a file /tmp/x.txt with hello"}],
  "tools": [{"type":"function","function":{"name":"write","parameters":{"type":"object","properties":{"filePath":{"type":"string"},"content":{"type":"string"}}}}}]
}'
```

Успех — `"finish_reason": "tool_calls"` и заполненный `message.tool_calls`.

---

## Переключение моделей, DeepThink и веб-поиск

| Что | Значение в opencode | Примечание |
| --- | --- | --- |
| Быстрая модель | `local-deepseek/deepseek-chat` | По умолчанию |
| Экспертная модель | `local-deepseek/deepseek-expert` | Сильнее, медленнее |
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

### Плагин дисциплины инструментов (важно для агента)

Из-за эмуляции tool calling (см. выше) модель склонна «описывать» действие
вместо вызова. Плагин добавляет позднюю системную инструкцию, требующую полного
блока `tool_calls`, который мост преобразует в API-вызовы. Файл
авто-подхватывается opencode без правки конфига:

- глобально: `~/.config/opencode/plugin/deepseek-tool-discipline.js`
- в проекте: `.opencode/plugin/deepseek-tool-discipline.js` (лежит в репозитории)

```js
export const DeepSeekToolDiscipline = async () => ({
  "experimental.chat.system.transform": async (input, output) => {
    if (input?.model?.providerID !== "local-deepseek") return;
    output.system.push(
      "To perform an action, your ENTIRE reply must be exactly one fenced " +
      "```tool_calls JSON array in the format specified by the bridge. " +
      "Do not add prose, XML, DSML, or pseudo-calls."
    );
  },
});
```

> Хуки `experimental.*` помечены экспериментальными (проверено на opencode
> 1.18.x). Если их переименуют, плагин просто перестанет срабатывать и не
> сломает запуск.

### DeepThink и веб-поиск (опционально)

`thinking`/`search` — нестандартные поля тела запроса; opencode их напрямую не
передаёт. Их можно добавить хуком `chat.params` (`output.options.thinking =
true`), но **не включайте reasoning по умолчанию** — он ухудшает соблюдение
формата tool calls. Через `curl`/SDK они работают сразу:

```python
resp = client.chat.completions.create(
    model="deepseek-expert",
    messages=[{"role": "user", "content": "Что нового в мире?"}],
    extra_body={"thinking": True, "search": True},
)
```

---

## Qwen Chat

Qwen подключается к тому же `/v1` отдельным провайдером `local-qwen`.
Он использует ваш аккаунт [chat.qwen.ai](https://chat.qwen.ai/) и его лимиты;
это не официальный API Alibaba Cloud и не авторизация Qwen Code. Доступность
моделей и квоты определяет веб-сервис. Поддержка выключена по умолчанию.

1. В активном Python-окружении выполните `python -m qwen.auth`. Войдите вручную
   в открывшемся окне. Если Google отклоняет автоматизированный браузер, используйте
   доступный на странице вход по коду на почту своего аккаунта.
2. Добавьте `QWEN_ENABLED=1` в `.env` и перезапустите сервер.
3. Добавьте следующий провайдер в существующий объект `provider` конфигурации
   opencode; затем перезапустите opencode и выберите модель через `/models`.

```json
"local-qwen": {
  "npm": "@ai-sdk/openai-compatible",
  "name": "Qwen Chat (local bridge)",
  "options": {
    "baseURL": "http://127.0.0.1:8000/v1",
    "apiKey": "unused"
  },
  "models": {
    "qwen3.8-omni-flash": {
      "name": "Qwen3.8 Omni Flash",
      "tool_call": true,
      "reasoning": false,
      "limit": { "context": 64000, "output": 8192 }
    },
    "qwen3.8-max": {
      "name": "Qwen3.8 Max",
      "tool_call": true,
      "reasoning": false,
      "limit": { "context": 64000, "output": 8192 }
    }
  }
}
```

Лимиты в примере — консервативный бюджет клиента, не обещание квоты веб-чата.
Плагин `.opencode/plugin/deepseek-tool-discipline.js` поддерживает оба провайдера;
обновите ранее установленную копию. Вызовы инструментов проходят через тот же
строгий парсер `tool_calls`. Проверены живые ответы, продолжение диалога,
выполнение `read` из opencode на обеих моделях в версии до защитных изменений.
Автоматическое headless-обновление теперь отключено.

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-omni-flash","messages":[{"role":"user","content":"Hello"}]}'

opencode run --model local-qwen/qwen3.8-max "Объясни этот проект"
```

Вход и профиль Qwen хранятся отдельно: `session/qwen/session.json` и
`session/qwen/profile/`. Сервер загружает только готовую сессию. При отсутствии
или истечении срока вернётся `401 login_required`, либо терминальная ошибка
`login_required` в уже начавшемся SSE-потоке: войдите вручную командой
`python -m qwen.auth`, затем перезапустите сервер. Отказ провайдера сохраняет
паузу; автоматического обновления или повтора нет.

Модели Flash и Max используют одну очередь аккаунта Qwen; очередь DeepSeek
независима. `conversation_id` Qwen содержит префикс `qwen:` и исходную модель;
диалог другого провайдера отвергается до обращения к аккаунту. Поддерживается
текст: изображения, аудио и видео не загружаются, несмотря на имя Omni.
Веб-протокол неофициальный и может измениться.

---

## Переменные окружения

Файл `.env` в корне репозитория (копия `.env.example`) загружается до чтения
настроек при запуске через `app.py`, `uvicorn`, `chat.py`, `deepseek.auth` и `qwen.auth`.
Уже заданные переменные окружения имеют приоритет.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Адрес прослушивания |
| `PORT` | `8000` | Порт |
| `RATE_LIMIT_PER_MINUTE` | `30` | Лимит запросов/мин на IP клиента (`/healthz` не считается) |
| `DEEPSEEK_PROFILE_DIR` | — | Переиспользовать существующий профиль Chrome с активной сессией |
| `DEEPSEEK_ENABLED` | `0` | Explicit DeepSeek opt-in; account restriction risk |
| `PROVIDER_MIN_INTERVAL` | `10` | Seconds between provider attempts/continuations; not a quota guarantee |
| `QWEN_ENABLED` | `0` | Включить модели Qwen Chat на том же `/v1` |
| `QWEN_PROFILE_DIR` | `session/qwen/profile` | Отдельный постоянный профиль Qwen Chat |
| `TOOLCALL_MAX_CONTINUATIONS` | `3` | Сколько раз допрашивать модель «продолжи», если tool call обрезан лимитом вывода |
| `DEBUG_TOOLCALLS` | — | `1` — писать в лог сырой ответ модели, если вызов не распознан |

Пример:

```bash
HOST=0.0.0.0 PORT=8080 RATE_LIMIT_PER_MINUTE=60 python app.py
```

---

## Ограничения и важные нюансы

- **Сериализация запросов.** Сервер выполняет запросы общего аккаунта
  **по одному для каждого провайдера**, включая стриминг и continuation (см. `server/api.py`).
  Ожидание очереди не занимает рабочие потоки. Прямой `DeepSeekClient` также
  сериализует генерации внутри одного экземпляра. Используйте один процесс
  сервера: несколько workers или отдельных клиентов не имеют общей очереди.
- **Tool calling эмулируется.** Мост буферизует ответ и парсит его в tool
  calls (`server/openai_format.py`, `parse_tool_calls`). Требуется один полный
  блок `tool_calls`; другие формы не исполняются. Поддерживаются `auto`, `none`,
  `required` и принудительный выбор объявленного инструмента. Невалидный
  результат возвращается ошибкой, в SSE — событием `error` перед `[DONE]`.
- **Обрыв ответа.** EOF без завершающего маркера и ошибки DeepSeek возвращаются
  как ошибки; ограничение длины ответа передаётся как `finish_reason: "length"`.
- **Нет реального подсчёта токенов.** `usage` — грубая оценка ~4 символа/токен.
- **Большинство OpenAI-параметров игнорируется** (`temperature`, `top_p`,
  `max_tokens` и т.д.). Действуют только `model`, `messages`, `stream`,
  `conversation_id`, `thinking`, `search`, `tools`, `tool_choice`.
- **Vision не поддерживается** (нет загрузки изображений).
- **Идентификатор диалога.** `conversation_id` фиксирует модель при создании
  потока; при продолжении `model` игнорируется.
- **Лимиты.** Локальный rate limit и квота веб-сайта независимы. При
  исчерпанной квоте остановите запросы; автоматические повторы SDK могут
  повторно расходовать allowance. Для ручной диагностики задайте `max_retries=0`.
- **Не хаммерьте аккаунт.** Это ваш обычный аккаунт DeepSeek; массовые
  автоматические запросы могут привести к блокировке.

---

## Обслуживание и troubleshooting

**Сессия истекла / `401 login_required`**

```bash
python -m deepseek.auth   # войдите заново
```

Выполните вход заранее и перезапустите сервер. Если провайдер приостановлен,
вход не снимает паузу; сначала проверьте доступ в обычном браузере.

**`Playwright Sync API inside the asyncio loop`**

Не вызывайте синхронный Playwright из event loop — мост уже оборачивает вызовы
в `run_in_threadpool`. Такое сообщение обычно означает нестандартную
модификацию кода.

**Ошибки PoW / wasmtime**

```bash
pip install --upgrade wasmtime
```

На macOS Python 3.9 из Xcode может завершаться с `EXC_GUARD` при загрузке
WASM, без Python traceback. В таком случае создайте новое окружение на
отдельно установленном Python 3.12 и переустановите зависимости. Сохранённый
вход в `session/` можно использовать повторно. Проверьте загрузку модуля
до запуска сервера:

```bash
python -c "from deepseek.pow import DeepSeekPow; DeepSeekPow()"
```


**`429 Too Many Requests`**

Проверьте источник отказа. Локальный лимитер возвращает `type: rate_limit_error`
и `Retry-After`: дождитесь указанного интервала. Квота сайта — отдельное
ограничение аккаунта; увеличение `RATE_LIMIT_PER_MINUTE` её не меняет. При
сообщении сайта о лимите остановите запросы и проверьте тариф/сброс квоты.
[Подробнее](../browser/README.md#usage-limits).

**Порт занят**

```bash
PORT=8080 python app.py
```

и укажите `http://127.0.0.1:8080/v1` в `baseURL` провайдера opencode.

**Агент отвечает текстом, но файлы не меняет (нет tool calls)**

1. В `opencode.json` у моделей должно быть `"tool_call": true`.
2. Проверьте мост напрямую `curl`-запросом с `tools` (см.
   [Интеграция с opencode](#интеграция-с-opencode)): должен вернуться
   `"finish_reason": "tool_calls"` и непустой `message.tool_calls`.
3. Поставьте плагин `deepseek-tool-discipline.js` (глобально или в проект).
4. Запустите сервер с `DEBUG_TOOLCALLS=1` и изучите сырой ответ модели в логе.
5. Увеличьте `steps` у агента и используйте `deepseek-expert`.

**Правки кода не применились после редактирования (launchd)**

Сервер под launchd с `reload=False` — перезапустите его:
`launchctl kickstart -k "gui/$(id -u)/com.deepseek.api"`.

**Изменения в `opencode.json`/агентах не применились**

Перезапустите opencode — конфиг не перезагружается на лету.

---

## Структура проекта

| Путь | Назначение |
| --- | --- |
| `app.py` | Точка входа — запускает сервер |
| `settings.py` | Загрузка `.env` до чтения настроек |
| `deepseek/` | Ядро: `DeepSeekClient`, вход (`auth.py`), HTTP-драйвер (`client.py`), PoW (`pow.py`) |
| `qwen/` | Опциональный Qwen Chat: отдельный вход, HTTP API v2 и SSE |
| `chat_protocol.py` | Общие границы SSE-событий и результат завершённого чата |
| `server/` | FastAPI OpenAI-совместимый сервер (`api.py`, `config.py`, `openai_format.py` — парсер tool calls, `ratelimit.py`, `schemas.py`) |
| `.opencode/plugin/` | Плагин дисциплины инструментов для opencode |
| `examples/` | Запускаемые примеры (прямой Python и через сервер) |
| `session/` | Сохранённая сессия (cookies + токен), **git-ignored** |
| `logs/` | Логи launchd-сервиса, **git-ignored** |
| `.env.example` | Шаблон конфигурации |
| `requirements.txt` | Python-зависимости |
| `tests/` | Offline-регрессии: auth, tools, SSE, API, CLI и rate limit |

Проверка без аккаунта DeepSeek и браузера:

```bash
python -m unittest discover -s tests -t . -v
python -c "from deepseek.pow import DeepSeekPow; DeepSeekPow()"
bash -n ds bin/ds-chat
node --check .opencode/plugin/deepseek-tool-discipline.js
```

GitHub Actions запускает эти проверки на Python 3.9 и 3.12.

---

## Безопасность

- Всё в `session/` (cookies + bearer-токен) остаётся **на вашей машине** и
  исключено из git (`.gitignore`). Никогда не коммитьте `session/`.
- На POSIX файлы сессии и состояния чата записываются атомарно с правами `0600`,
  их каталоги и профиль браузера — `0700`. Для каждого провайдера сохраняются
  только его cookies с ограничениями домена, пути, срока действия и HTTPS.
- Старый кеш с cookies в виде словаря больше не используется: мост повторно
  захватит сессию из профиля. Если это не удалось, выполните
  `python -m deepseek.auth` и перезапустите сервер.
- Пароли/секреты в `.env` не хранятся — вход выполняется вручную в браузере.
- При `HOST=0.0.0.0` мост доступен в сети без аутентификации. Биндитесь только
  на `127.0.0.1` или закрывайте firewall'ом/прокси.
- `app.py` не доверяет forwarded-заголовкам; rate limit использует адрес
  соединения из ASGI. При запуске через uvicorn используйте
  `--no-proxy-headers`. За доверенным reverse proxy разрешайте proxy headers
  только для его IP через `--forwarded-allow-ips`; не задавайте `*`.
- Не публикуйте `session/` и `.env` в публичных репозиториях.

---

## Лицензия

[MIT License](../LICENSE). Проект неофициальный; вы отвечаете за соблюдение
условий использования DeepSeek.

**Оригинал:** <https://github.com/sums001/Deepseek-API>
