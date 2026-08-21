# NEXT_STEPS — что делать дальше (обновлено 2026-08-21)

Три завершённых блока рефакторинга в main/project-bug-fix:
1. utcnow -> naive-UTC clock seams (все прод-файлы).
2. Data-move: справочники локаций вынесены из кода в JSON.
3. Импортируемость: `main.py` и `panel_bot.py` больше не имеют
   import-time side effects, ломавших `import` (см. п.0).

## 0. Импортируемость main.py / panel_bot.py (НОВОЕ, 2026-08-21)

Раньше `import main` и `import panel_bot` падали без полного live-env
(валидные API_ID/API_HASH, session, manager registry) — Telethon строил
клиент прямо на уровне модуля, и все селфтесты были вынуждены извлекать
код через AST (`ast.parse`+`exec`) вместо обычного `import`.

- `main.py`: bootstrap (creds-check, proxy-resolve, `TelegramClient(...)`,
  регистрация корневого хендлера) вынесен в `_tp_bootstrap_runtime()`,
  вызывается ТОЛЬКО из `__main__`. На module level безопасные дефолты
  (`client = None`). Единственный `@client.on` заменён на
  `add_event_handler` внутри bootstrap.
- `panel_bot.py`: 63 `@client.on(...)` декоратора сохранены дословно;
  на import `client` — лёгкий рекордер `_PanelDeferredClient`, который
  запоминает регистрации В ПОРЯДКЕ ИСХОДНИКА. `_pb_bootstrap_runtime()`
  (только из `__main__`) строит реальный клиент, воспроизводит
  регистрации через `add_event_handler` и переназначает глобал `client`.
  Порядок регистрации критичен для Telethon (диспетчеризация в порядке
  добавления) — проверено симуляцией: order preserved = True, 63/63.
- Import-time установка `os.environ` НЕ трогалась намеренно: ниже по
  модулю ~48 module-level чтений env, перенос порядка инициализации —
  отдельная более рискованная задача (НЕ входит в этот блок).

Проверка: `import main` и `import panel_bot` через venv проходят без
кредов; диффы против HEAD — ровно целевые правки; mojibake 0.
ВАЖНО на сервере: боевой запуск `python main.py <manager>` и
`python panel_bot.py` подтверждает владелец — это меняет прод-точку входа.

## 1. Деплой на сервер (ПРИОРИТЕТ)

Деплой-пакет: `main.py`, `panel_bot.py`, `storage.py`, `manager_bot.py`,
`partner_stat_bot.py`, `panel_bridge.py`, `liquid_ru_locations.py`,
`non_liquid_locations.py`, каталог `data/` (ru_locations.json,
non_liquid_locations.json) + изменённые `tools/*_selftest.py`.
ВНИМАНИЕ: без каталога `data/` боты не стартуют (shim'ы локаций читают
JSON при импорте).

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

## 2b. Data-move справочников локаций ЗАВЕРШЁН (2026-08-21)

Два огромных data-файла заменены тонкими shim-загрузчиками (данные в JSON):
- `liquid_ru_locations.py`: 159 775 -> 47 строк; данные в
  `data/ru_locations.json` (17.9 MB, 159 765 записей). Shim читает JSON и
  реконструирует `RU_LOCATIONS` как `dict[str, tuple[str, tuple]]`
  байт-в-байт. Потребители (`geo_lexicon`, `router`) не менялись.
- `non_liquid_locations.py`: 45 591 -> 396 строк; 5 крупных литералов
  (`ABBR_SHORT`, `COUNTRIES`, `PLACES_POPULAR`, `PLACES_RARE`,
  `_POST_SOVIET_CITIES5000`) в `data/non_liquid_locations.json` (2.4 MB).
  ВСЯ логика (циклы post-soviet merge, `ABBR.update`, `_ps_*`) сохранена;
  `router.py` (dir()+getattr harvest) видит идентичную поверхность модуля.

Паритет-тесты (байт-в-байт, ключи/значения/типы-кортежи/порядок):
`tools/liquid_ru_locations_parity_selftest.py`,
`tools/non_liquid_locations_parity_selftest.py` — оба PASS
(267 302 записи суммарно). Полный прогон 139/152, 0 регрессий.

Побочно: `.bak_*` бэкапы убраны из git-индекса и добавлены в `.gitignore`
(раньше несколько 45k-160k-строчных бэкапов случайно попадали в коммиты).

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
