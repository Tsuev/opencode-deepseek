"""Local recovery completion, distinct from provider/protocol failure."""

from chat_protocol import Reply

RECOVERY_STOP_TEXT = (
    "Сохранённый ответ передан. Восстановление завершено; продолжение "
    "остановлено локально без новой генерации. Цель не объявлена выполненной. "
    "Автоматически продолжать её не нужно."
)


def recovery_stopped():
    # Do not attach a provider CID or remember this local message as a remote
    # reply. This completes only the recovery turn, never the user's goal.
    content = RECOVERY_STOP_TEXT
    return Reply(content, None, "stop"), content, None
