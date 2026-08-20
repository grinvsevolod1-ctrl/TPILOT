# NEXT_STEPS — что делать дальше (после utcnow-рефакторинга, 2026-08-16)

Состояние на момент пуша в main: рефакторинг `datetime.utcnow()` -> naive-UTC
clock seams ЗАВЕРШЁН для ВСЕХ прод-файлов (этап 1 + этап 2):
- Этап 1: `storage.py` (`_utc_now`), `panel_bot.py` (`_pb_utc_now`),
  `main.py` (`_tp_utc_now`, 50 мест).
- Этап 2: `manager_bot.py` (`_mbot_utc_now`, 9), `partner_stat_bot.py`
  (`_psb_utc_now`, 6), `panel_bridge.py` (`_pbr_utc_now`, 3).

Валидация этапа 2: полный прогон 137/150 PASS, 0 регрессий — все 13
падений идентичны pre-existing baseline (см. п.4). allow_spend AST-гейт
PASS, mojibake 0 во всех изменённых файлах, py_compile всех прод-файлов OK.
В прод-коде не осталось ни одного вызова `datetime.utcnow()` — только
упоминания в комментариях/докстрингах (описывают контракт seam'ов).

## 1. Деплой на сервер (ПРИОРИТЕТ)

Деплой-пакет: `main.py`, `panel_bot.py`, `storage.py`, `manager_bot.py`,
`partner_stat_bot.py`, `panel_bridge.py` + изменённые `tools/*_selftest.py`
(селфтесты опциональны в рантайме, но нужны для валидации на сервере).

Порядок по проектному регламенту (AGENTS.md, раздел 10):
1. Бэкап целевых файлов на сервере (`<file>.py.bak_utcseam_<timestamp>`).
2. Копировать файлы в `C:\ALM_TPilot`.
3. На сервере: `python -m py_compile main.py panel_bot.py storage.py`.
4. Прогнать offline-селфтесты из `tools\` (без сети, без спенда).
5. Рестарт контроллера/ботов, проверить логи на DeprecationWarning
   (их больше быть не должно) и на NameError вокруг `_tp_utc_now` /
   `_pb_utc_now` / `_utc_now`.
   6. Ручная проверка ботов: карточка менеджера, proxy pool, статистика.
   7. Рестарт бот-процессов, использующих новые seam'ы: ManagerBot
      (`_mbot_utc_now`), PartnerBot (`_psb_utc_now`), panel bridge
      (`_pbr_utc_now`) — проверить логи на NameError вокруг этих имён.

## 2. Рефакторинг utcnow ЗАВЕРШЁН

Все прод-файлы переведены на naive-UTC clock seams. Больше нет файлов
с `datetime.utcnow()` в исполняемом коде. Сводка seam'ов по файлам:
- `storage.py` — `_utc_now`
- `panel_bot.py` — `_pb_utc_now`
- `main.py` — `_tp_utc_now`
- `manager_bot.py` — `_mbot_utc_now`
- `partner_stat_bot.py` — `_psb_utc_now`
- `panel_bridge.py` — `_pbr_utc_now`

Все seam'ы имеют идентичный контракт: `datetime.now(timezone.utc)
.replace(tzinfo=None)` — naive datetime в UTC, байт-в-байт совместимый
со старым `datetime.utcnow()`, но без DeprecationWarning на Python 3.12+.
Если понадобится добавить новый вызов времени — использовать seam
своего модуля, НЕ `datetime.utcnow()`.

## 3. Известные грабли этого рефакторинга (для продолжения работ)

- Обработчики в panel_bot/main часто глотают исключения (`try/except: pass`):
  NameError по отсутствующему seam в extraction-ns проявляется НЕ как
  traceback, а как «тихо не записалось / кнопка не сработала» в тесте.
- Правки русскоязычных файлов через shell heredoc/sed ломают кодировку.
  Только побайтовые правки через python-скрипт с `encoding="utf-8"` и
  обязательный mojibake-скан (`grep -c "�"`) после КАЖДОЙ правки.
- Бэкап `main.py.bak_utcseam_20260816_051110` — эталон для восстановления
  строк, если снова обнаружится порча кодировки.

## 4. Pre-existing падения селфтестов (НЕ от рефакторинга)

Финальный прогон этапа 2: 137/150 PASS. Все 13 падений — pre-existing
(FAIL и в baseline до рефакторинга, сверено с прогоном ph7). НИ ОДНОЙ
регрессии от utcnow-рефакторинга. Причины — отсутствие на этой рабочей
машине артефактов, которые есть только на сервере/в release-сборке:

- `rc_scope_guard`, `w3_1_scope_guard`, `w3_2_scope_guard`,
  `w3_3_scope_guard`, `w3_3_b_scope_guard`, `w3_4_scope_guard`,
  `w3_2_d6d7_correction_scope_guard` — ищут release-бэкапы / артефакты
  по путям вида `C:\ALM_TPilot_AUDIT\...`, которых нет в рабочей копии.
- `startup_isolation` — падал в baseline, требует отдельной диагностики.
- `w3_2_business_date_fallback`, `w3_3_c1_reader_parity`,
  `w1_proxy_guard_sweep_observability` — зависят от тех же отсутствующих
  audit-артефактов.
- `tg_health_peerflood_failclosed`, `tg_health_recovery` — pre-existing,
  требуют окружения/фикстур, недоступных локально.

Ни одно из этих падений не блокирует деплой utcnow-патча. На сервере, где
audit-артефакты присутствуют, их надо перепрогнать для подтверждения.

## 5. Отложенные не-блокирующие задачи (из AGENTS.md, раздел 7)

- `proxy_renew_notify_purge_old` существует, но нигде не вызывается —
  notify-log растёт неограниченно (безвредно). Подключить к периодическому
  циклу контроллера.
- Сравнение PIN в password-reveal не constant-time — заменить на
  `hmac.compare_digest`.
- Проверить на сервере, что задан `PANEL_ADMIN_PASSWORD`
  (или `MANAGER_ADMIN_PASSWORD`) — иначе password reveal всегда отказывает.
- AdminBot runtime-stats stage — статус неясен, сверить с сервером.
- Bulk "Ссылки на дату" wizard (bld:) + buyer-request push-card — план был,
  реализация/деплой не подтверждены, сверить с сервером перед новыми работами.

## 6. Гигиена репозитория

- В рабочей копии много `*.bak_*` файлов — они намеренно НЕ закоммичены
  (локальная страховка). После подтверждённого деплоя можно чистить.
- Ветка `project-bug-fix` содержит всю историю рефакторинга; `main` обновлён
  этим пушем. База `codebase-audit` — предыдущий этап аудита.
