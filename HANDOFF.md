# TPilot — передача работ (ветка codebase-audit, 2026-08-15)

Файл-передача для продолжения работы в следующих чатах.
Репозиторий: grinvsevolod1-ctrl/TPILOT, ветка `codebase-audit` (base: `main`).

ВАЖНО: старый AGENTS.md в корне устарел по решению владельца — его правила
(режимы Codex, ручные .bak-бэкапы, «не git-репозиторий») больше не действуют.
Актуальный источник правды — этот файл + git-история ветки codebase-audit.

---

## 1. Что сделано (все коммиты в codebase-audit)

### Этап A. Исправление багов (11 коммитов)

| Коммит | Что исправлено |
|---|---|
| json-alias fix | `NameError: json` в `replacement_confirm_tdimport_install` (main.py) — тихо ломал tdata-импорт (except глотал ошибку) |
| `_tdimport_display_username` | `NameError: _display_username` в экране подтверждения tdimport (panel_bot.py ~24620) — крашил экран для аккаунтов с username |
| imports fix | Нормальный `import shutil` вместо инъекции `globals()["shutil"]`; добавлены `Sequence` (storage.py), `Optional` (manager_bot.py) |
| 497fefe | 6 f-строк без плейсхолдеров (косметика) |
| f40805a | Constant-time сравнение PIN (`hmac.compare_digest` через `_ppool_pin_matches`) в panel_bot.py |
| 0f9fc3e | closer_settings_selftest: извлекал не ту версию override после W1-блока — добавлен параметр `containing=` |
| 3e6d030 | deleted_manager_stats_retention_selftest: хардкод дат (окно 60 дней истекло 31.07.2026) — даты стали относительными к now() |
| 7872b4a | manager_card_unified_selftest: не знал о badged-резолвере из PEERFLOOD 20260812 |
| 3b5ccc8 | devlogin_adminbot_wiring_selftest: принимает constant-time PIN-сравнение |
| ad39d79 | bizlink_delete_result + canonical_back_navigation: SKIP вместо FAIL при отсутствии pre-git *.bak_* файлов |
| 05bfb3f | Восстановление UTF-8 (U+FFFD артефакты от edit-инструмента) в main.py/panel_bot.py |
| 166454b | tools/__pycache__ и __pycache__ убраны из git, добавлены в .gitignore |
| 8203d7a | manager_runtime_start_selftest: SKIP pre-git backup-проверки (tripwire-4) |

После этапа A: **40/40 селфтестов PASS**.

### Этап B. Рефакторинг — чистка override-цепочек (выполнено)

Инструменты (добавлены в tools/):
- `tools/override_reachability_report.py` — консервативный AST-анализатор:
  находит перекрытые (shadowed) определения top-level функций и классифицирует
  dead / live. Правила безопасности: определение удаляется ТОЛЬКО если между
  ним и его переопределением нет `globals().get("имя")`-снапшота, оно без
  декораторов, имя нигде не биндится присваиванием на уровне модуля, и имя
  не входит в exclude-список (извлекаемые селфтестами). Всё сомнительное = live.
  Пишет `/tmp/override_dead_spans_<file>.json`.
- `tools/override_dead_code_delete.py` — удаляет спаны по номерам строк
  (байтово-безопасно, без ручных правок UTF-8).

Результаты чистки (все PREV-захваченные версии сохранены):

| Файл | Удалено мёртвых def | Строк |
|---|---|---|
| partner_stat_bot.py (efe4937) | 37 | −740 |
| manager_bot.py (b8ab80c) | 5 | −144 |
| panel_bot.py (72fd19a) | 27 | −531 |
| main.py (ef4fb8d) | ~296 | ~−9800 |

Обновлённые под чистку селфтесты (8603b6b и рядом):
- canonical_back_navigation: `_ACTIVE_NOT_LAST["_followups_menu"]` = **-2**
  (отрицательный индекс от конца, работает до и после чистки)
- hnv2_classifier / hnv2_flap_watermark / hnv2_lifecycle:
  `_send_manager_private` ожидание 4 → **3**
- b1b2_correction PH-T10: call sites `_upsert_card_placeholder` 2 → **1**

### Валидация на момент передачи
- py_compile всех 26 модулей — OK
- pyflakes: 0 undefined names
- mojibake (U+FFFD, Ð, Ñ, â€): 0
- allow_spend AST-gate: PASS (ровно 2 `allow_spend=True` в main.py)
- Полный прогон селфтестов после чистки main.py: последний зафиксированный
  результат — все PASS, кроме списка «pre-existing» ниже (п. 3)

---

## 2. Что осталось доделать

ОБНОВЛЕНО 2026-08-15 (позднее): пункты 1–3 ВЫПОЛНЕНЫ.

1. ~~Финальный контрольный прогон~~ — ВЫПОЛНЕНО: полный batch-прогон,
   36+ PASS; бывшие «pre-existing» провалы manager_relogin / menu_parity /
   manager_replacement_adminbot теперь PASS (падали из-за грязного дерева
   во время экспериментов, не багов).
2. ~~Диагностика pre-existing провалов~~ — ВЫПОЛНЕНО:
   - manager_delete_safety: причина — pre-git backup-файл, теперь SKIP (bcd32bb)
   - auto_status_disable: стаб _DummyDatetime не знал .now() после
     utcnow-миграции — дополнен (bcd32bb)
   - bizlink_readiness_integration: проходит с таймаутом 500с (медленный
     тест, ~5-6 мин; timeout 150 в batch-прогонах даёт ложный FAIL(124))
3. ~~Замена datetime.utcnow()~~ — ВЫПОЛНЕНО (cf66a21): весь продуктовый код
   (main, storage, panel_bot, manager_bot, partner_stat_bot, panel_bridge,
   profile_dialog, preflight_check, refresh_manager_profiles,
   repair_reparse...) → `datetime.now(__import__("datetime").timezone.utc)
   .replace(tzinfo=None)`. Самодостаточная __import__-форма ОБЯЗАТЕЛЬНА:
   селфтесты extract+exec функции в изолированных namespace, где новые
   module-level имена дают NameError (проверено: alias _TZ_UTC сломал 13
   тестов). В tools/*.py utcnow оставлен намеренно (тестовый код, только
   DeprecationWarning).
4. **Push + PR** ветки codebase-audit → main (ждёт команды владельца).
5. **Новый AGENTS.md** — владелец хочет написать новые правила проекта с нуля
   после завершения работ (git-workflow вместо .bak-файлов).
6. Опционально: разбиение main.py (~26k строк после чистки) на модули —
   владелец выбрал «чистка без разбиения», разбиение отложено.
7. Опционально: миграция utcnow в tools/*.py (только предупреждения).

---

## 3. Подсказки для следующих чатов

### Окружение
- Рабочая директория: `/vercel/share/v0-project`
- Системный python3 — БЕЗ зависимостей. Для селфтестов используй venv:
  `/tmp/audit-venv/bin/python` (aiosqlite, python-dotenv, telethon, pyflakes).
  Если venv пропал: `uv venv /tmp/audit-venv && uv pip install --python
  /tmp/audit-venv/bin/python aiosqlite python-dotenv telethon pyflakes`
- Селфтесты: `for t in tools/*selftest*.py; do timeout 150
  /tmp/audit-venv/bin/python "$t"; done` (полный прогон ~10-15 мин,
  запускай в фоне)
- `bizlink_readiness_integration_selftest` идёт ~5-6 мин — не считай
  таймаут за провал.

### Архитектурные ловушки (по-прежнему актуальны)
- main.py и panel_bot.py содержат stacked overrides: активна ТОЛЬКО ПОСЛЕДНЯЯ
  top-level def с данным именем. Ищи через `grep -n "def имя"` и бери последнюю.
- Паттерн цепочки: `_X_PREV = globals().get("имя")` перед переопределением —
  такие PREV-версии ЖИВЫЕ, удалять нельзя.
- main.py/panel_bot.py НЕ импортируются напрямую (Telethon/env side effects).
  Селфтесты извлекают код через ast.parse → ast.unparse → exec. Panel_bot и
  main — разные процессы: общих имён нет, только panel_commands в БД.
- Многие селфтесты проверяют ТОЧНЫЕ counts определений/вызовов и извлекают
  функции по индексу или маркеру. Любое удаление/добавление def может сломать
  тест — это чаще устаревший тест, а не баг кода. Сравнивай поведение на
  дереве до/после (`git stash` / `git show COMMIT:file > file`).

### Инварианты безопасности (проверять после правок в этих зонах)
- `allow_spend=True` — ровно 2 места в main.py (buy-confirm make_ipv4 и
  prolong_make в `_prenew_execute_renewal`). Гейт: `python3
  tools/allow_spend_ast_gate.py` → RESULT: PASS.
- Пароль прокси никогда не попадает в panel_commands/result_text/JSON —
  только `has_password`; reveal = локальное чтение sqlite после PIN
  (`_ppool_pin_matches`, constant-time).
- Первое сообщение клиента — никогда не profile evidence.
- Ночные лиды/долёты: окно 17:00–08:00 только для спец-режимов статистики.

### Правила работы (новые, вместо старого AGENTS.md)
- Git — единственный механизм отката. Никаких .bak-файлов.
- Один коммит = одно логическое исправление, трейлер
  `Co-authored-by: v0 <it+v0agent@vercel.com>`.
- После каждой правки: py_compile → pyflakes (0 undefined) → mojibake-скан
  (`grep -c "�"` = 0) → allow_spend-gate → затронутые селфтесты.
- НЕ редактировать русские строки в main.py/panel_bot.py вручную большими
  кусками — были случаи порчи UTF-8 (лечится: `git show base:file > file` +
  повторное применение фиксов скриптом).
- Файлы `*_cursor_copy.py`, `main_beka*.py`, `*.bak_*` — мёртвые копии,
  не редактировать.

### Известные некритичные хвосты
- PIN-сравнение теперь constant-time, но PANEL_ADMIN_PASSWORD должен быть
  задан на сервере (иначе reveal всегда «PIN не настроен» — это безопасно).
- `proxy_renew_notify_purge_old` подключён в ежедневный housekeeping
  (main.py ~33319) — проверено, работает.
