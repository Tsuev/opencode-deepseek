# DeepSeek в терминале и в Kaku

Полная инструкция: как запустить чат с DeepSeek одной командой и как получать
DeepSeek по `Command+L` прямо в терминале Kaku.

Мост работает на **вашем обычном аккаунте** [chat.deepseek.com](https://chat.deepseek.com)
без API-ключа и оплаты — поэтому для первого запуска нужна ручная авторизация
в браузере.

---

> Автоматизация веб-чата может ограничить аккаунт. Текущий тестовый аккаунт
> DeepSeek заблокирован; точная причина неизвестна. CLI использует сохранённую
> сессию без автоматического входа и общий файл пауз `session/provider-pauses.json`.
> При отказе остановитесь и проверяйте обычный сайт вручную. Вход не снимает паузу.

## Коротко

| Что | Как |
| --- | --- |
| Чат в терминале | `./ds` (или `ds-chat` после установки shim) |
| Чат в Kaku | `Command+L` |
| Мост для других программ | `http://localhost:8000/v1` (OpenAI-совместимый) |

---

## 1. Что уже установлено

| Компонент | Путь | Назначение |
| --- | --- | --- |
| Репозиторий | `~/git/opencode-deepseek` (или ваш clone) | код |
| Окружение | `venv/` | Python 3.12 + зависимости |
| Launcher | `ds` | запуск REPL-чата |
| Shim (опционально) | `bin/ds-chat` → `~/bin/ds-chat` | запуск из любой директории |
| Автозапуск моста | `~/Library/LaunchAgents/com.deepseek.bridge.plist` | поднимает `app.py` при входе в систему |
| Конфиг Kaku | `~/.config/kaku/assistant.toml` | провайдер для `Command+L` |
| Сессия | `session/session.json` | сохранённый вход |
| Состояние чата | `session/chat_state.json` | текущий диалог и настройки |

---

## 2. Разовая установка

Нужно только на чистой машине. Если каталоги выше уже есть, этот раздел можно
пропустить.

```bash
cd ~/git/opencode-deepseek   # или путь к вашему clone

# 1) Окружение и зависимости
/opt/homebrew/bin/python3.12 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python -m playwright install chromium

# 2) Конфиг (API-ключ не нужен)
cp .env.example .env

# 3) (опционально) shim для запуска из любой директории
mkdir -p ~/bin
ln -sf "$(pwd)/bin/ds-chat" ~/bin/ds-chat
# убедитесь, что ~/bin в PATH, либо добавьте в ~/.zshrc:
#   alias ds-chat='~/bin/ds-chat'
```

### Проверка, что всё на месте

```bash
./venv/bin/python -c "import server.api; print('bridge ok')"
```

---

## 3. Авторизация (один раз)

Мост логинится в веб-чат DeepSeek. Откроется браузер — **войдите вручную**
(QR/Google/почта), 2FA пройдите сами. Сессия сохранится в `session/session.json`
и автозапуск больше не понадобится.

```bash
./venv/bin/python -m deepseek.auth
```

Признак успеха в конце вывода:

```
[auth] session saved to .../session/session.json
```

Если в выводе есть строка вида `could not fetch some cookies` — это
предупреждение, а не ошибка: сессия всё равно сохранена и работает.

---

## 4. Чат в терминале

```bash
./ds                 # продолжить текущий диалог
./ds --new           # начать новый диалог
```

После установки shim (см. раздел 2) то же самое из любой директории:

```bash
ds-chat
ds-chat --new
```

### Флаги запуска

| Флаг | Действие |
| --- | --- |
| `--new` | новый диалог |
| `--continue` | продолжить сохранённый диалог (по умолчанию) |
| `--model default` | быстрая модель Instant (`deepseek-chat`) |
| `--model expert` | более сильная и медленная Expert (`deepseek-expert`) |
| `--think` / `--no-think` | включить/выключить DeepThink-рассуждения |
| `--search` / `--no-search` | включить/выключить веб-поиск |

### Команды внутри чата

| Команда | Действие |
| --- | --- |
| `/new` | начать новый диалог |
| `/model default` | переключить на быструю Instant |
| `/model expert` | переключить на более сильную Expert (начинает новый диалог) |
| `/think` | переключить DeepThink-рассуждения |
| `/search` | переключить веб-поиск |
| `/status` | диалог, модель, рассуждения, поиск |
| `/help` | список команд |
| `/exit` | выйти |

Модель и режимы **сохраняются** в `session/chat_state.json`, поэтому следующий
запуск продолжает их. Модель фиксируется при создании диалога: смена модели
начинает новый диалог, в том числе через `--model`. Прерывание ответа или ошибка
сети сохраняют предыдущий `conversation_id`; следующий запрос продолжает
последний успешно завершённый ответ.

---

## 5. `Command+L` в Kaku

`Command+L` открывает встроенный AI-чат Kaku, и он направлен на локальный
мост DeepSeek.

Как это устроено:

```
Kaku (Command+L)  ──►  http://localhost:8000/v1  ──►  chat.deepseek.com
                       (app.py, launchd, :8000)        (ваша сессия)
```

Конфигурация лежит в `~/.config/kaku/assistant.toml`:

```toml
enabled = true
api_key = "not-needed"          # мост игнорирует ключ
model = "deepseek-chat"         # быстрая модель для подсказок команд
chat_model = "deepseek-chat"    # чат по Command+L
chat_model_choices = ["deepseek-chat", "deepseek-expert"]
base_url = "http://localhost:8000/v1"
```

`chat_model_choices` включает переключение моделей прямо в оверлее Kaku:
`deepseek-chat` — быстрые ответы, `deepseek-expert` — сильнее и медленнее.

Kaku перечитывает конфиг автоматически. Проверить: нажмите `Command+L` — должен
открыться AI-чат.

### Откат на OpenAI

Исходные значения сохранены рядом в комментариях и в
`~/.config/kaku/assistant.toml.bak`. Достаточно вернуть:

```toml
api_key = "<ваш ключ>"
model = "gpt-5.4-mini"
chat_model = "gpt-5.5"
base_url = "https://api.openai.com/v1"
```

и убрать `chat_model_choices`.

### Нюанс: подсказки команд

`enabled = true` включает не только чат, но и анализ вводимых команд — на каждую
команду уходит дополнительный запрос к DeepSeek. Если это не нужно, оставьте
`enabled = true`, но отключите авто-подсказки в настройках Kaku (раздел AI).

---

## 6. Мост для других программ

Мост поднимается автоматически при входе в систему. Проверка:

```bash
curl -s http://localhost:8000/healthz     # {"status":"ok"}
curl -s http://localhost:8000/v1/models   # deepseek-chat, deepseek-expert
```

Пример запроса (совместим с любым OpenAI-клиентом):

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer not-needed' \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"Привет"}]}'
```

Для opencode это провайдер с `baseURL = http://localhost:8000/v1` и любым
непустым API-ключом — см. [интеграцию с OpenCode](SETUP.ru.md#интеграция-с-opencode).

---

## 7. Обслуживание

```bash
# Статус автозапуска
launchctl print gui/$(id -u)/com.deepseek.bridge | grep -E 'state|pid'

# Логи
tail -f ~/Library/Logs/deepseek-bridge.log ~/Library/Logs/deepseek-bridge.err.log

# Перезапуск после правок
launchctl bootout gui/$(id -u)/com.deepseek.bridge
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.deepseek.bridge.plist

# Остановить / запустить вручную
launchctl bootout gui/$(id -u)/com.deepseek.bridge
./venv/bin/python app.py
```

### Частые проблемы

| Симптом | Причина и решение |
| --- | --- |
| `Command+L` не открывает чат | не поднят мост: `curl http://localhost:8000/healthz`, затем перезапустите launchd-агент |
| Мост отвечает, но чат молчит | нет сети через прокси: проверьте, что `7897` слушает (`lsof -i :7897`) |
| `ImportError: ... 'socksio' package is not installed` | `./venv/bin/pip install 'httpx[socks]'` |
| `DeepSeekClient` не проходит авторизацию | `./venv/bin/python -m deepseek.auth` заново |
| Порт 8000 занят | `lsof -ti :8000 | xargs kill -9` |

### Прокси

Веб-версия DeepSeek доступна только через локальный прокси
(`http://127.0.0.1:7897` + `socks5://127.0.0.1:7897`). Прокси прописан прямо в
launchd-plist: GUI-приложения и launchd не наследуют переменные из `.zshrc`.
Файл `.env` в репозитории содержит только комментарии — править его не нужно.

Если прокси у вас на другом порту, поменяйте `7897` в трёх местах:

1. `~/Library/LaunchAgents/com.deepseek.bridge.plist` → `EnvironmentVariables`
2. `bin/ds-chat` / `~/bin/ds-chat` (если установлен) — при необходимости задайте
   `http_proxy` / `https_proxy` / `all_proxy` в окружении перед запуском
3. `~/.zshrc` → `http_proxy` / `https_proxy` / `all_proxy`

---

## 8. Как это устроено

| Слой | Код |
| --- | --- |
| REPL-чат | `chat.py` |
| Launcher | `ds`, `bin/ds-chat` |
| HTTP-мост | `app.py`, `server/api.py` |
| Клиент веб-чата | `deepseek/client.py` (SSE + PoW через Wasmtime) |
| Авторизация | `deepseek/auth.py` (Playwright) |

Состояние диалога: `session/chat_state.json`. Переписывается на диск при каждом
изменении, поэтому чат можно закрыть и продолжить позже.

## Оговорка

Неофициальный проект, не связанный с DeepSeek. Используется ваш обычный
аккаунт; вы несёте ответственность за соблюдение условий сервиса.
