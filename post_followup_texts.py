# -*- coding: utf-8 -*-
from __future__ import annotations

"""Soft post-dialog follow-up texts for ALM_TPilot / TPilot v2.

These texts are used only after a manager has already replied manually and the
client stopped answering. They must stay neutral by gender, light, non-formal,
and not look like a repeated template.
"""

from typing import Iterable, Set

POST_MANUAL_FOLLOWUP_TEXTS = [
    ("wave_start_01", "👋 Тихо возвращаю диалог наверх, чтобы он не потерялся среди сообщений."),
    ("wave_middle_01", "Тихо возвращаю диалог наверх 👋 Чтобы он не потерялся среди сообщений."),
    ("wave_end_01", "Тихо возвращаю диалог наверх, чтобы он не потерялся среди сообщений 👋"),
    ("pause_01", "Кажется, чат сделал небольшую паузу :) Можно продолжить с любого удобного места 👋"),
    ("tea_01", "Похоже, диалог ушёл на чай :) 👋 Возвращаю его аккуратно наверх."),
    ("archive_01", "Маленькое напоминание, чтобы диалог не ушёл в архив раньше времени 👋"),
    ("signal_01", "👋 Лёгкий сигнал в чат. Можно продолжить, когда будет удобно."),
    ("signal_end_01", "Лёгкий сигнал в чат. Можно продолжить, когда будет удобно 👋"),
    ("place_01", "Сообщение на месте, диалог тоже :) Можно продолжить отсюда 👋"),
    ("quiet_01", "Без суеты, просто поднимаю диалог выше 👋 Можно продолжить, когда будете на связи."),
    ("ping_01", "Проверка связи 👋 Диалог ждёт продолжения, всё на месте."),
    ("soft_01", "Аккуратно напоминаю о диалоге 👋 Можно вернуться к нему с любого сообщения."),
]


def _parse_sent_keys(raw: str | Iterable[str] | None) -> Set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {x.strip() for x in raw.split(",") if x.strip()}
    return {str(x).strip() for x in raw if str(x).strip()}


def choose_post_manual_followup_text(sent_keys: str | Iterable[str] | None = None, *, chat_id: int = 0, date_key: str = "", slot: str = "") -> tuple[str, str]:
    """Return (template_key, text), avoiding previously sent keys when possible."""
    used = _parse_sent_keys(sent_keys)
    pool = [x for x in POST_MANUAL_FOLLOWUP_TEXTS if x[0] not in used]
    if not pool:
        pool = list(POST_MANUAL_FOLLOWUP_TEXTS)
    seed = f"{int(chat_id or 0)}:{date_key}:{slot}:{len(used)}"
    idx = sum((i + 1) * ord(ch) for i, ch in enumerate(seed)) % max(1, len(pool))
    return pool[idx]


__all__ = [
    "POST_MANUAL_FOLLOWUP_TEXTS",
    "choose_post_manual_followup_text",
]
