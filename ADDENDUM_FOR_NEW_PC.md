# ADDENDUM_FOR_NEW_PC.md — TPilot migration addendum (2026-07-10)

Companion to `CLAUDE.md` (from `CLAUDE_NEW_PC\`). CLAUDE.md = rules; this file = detailed
current-state snapshot, traps, and continuation instructions that did not fit there.
Facts below were verified in code/tests on 2026-07-10 on the old PC. Line numbers are
approximate and WILL shift after any edit — always re-locate by grep, never trust them blindly.

## 1. Exact project state snapshot (2026-07-10)

DEPLOYED on server (verified working before this migration):
* Everything listed in CLAUDE.md §5 up to and including Proxy Pool Stage 6 (P1–P4)
  and Proxy Renew Stage 5.

IMPLEMENTED LOCALLY, NOT DEPLOYED:
* Proxy Pool Stage 6.1 (A–F) — full detail in §3 below. Final read-only review: PASS,
  0 blocking issues. Ready to package.

UNCERTAIN (verify on server before building on top — do NOT assume):
* "Transfers UX" pending files (panel_bot.py / manager_bot.py / storage.py) mentioned in an
  older plan — may or may not already be on the server.
* Bulk "Ссылки на дату" wizard (`bld:` namespace) + buyer-request push-card buttons — was
  planned; implementation/deploy status unknown.
* AdminBot runtime-stats stage — was pending earlier; status unknown.
* Whether the new PC's `.env.TPilot` copy has redacted or real values — check length only,
  never print values.

## 2. Stage 6.1 — what exactly changed (the not-yet-deployed diff)

Changed files: `main.py`, `panel_bot.py`, `storage.py`, `tools\proxy_pool_selftest.py`.
Deploy package: ONLY `main.py`, `panel_bot.py`, `storage.py` (selftest is dev-only).
Backups (suffix `bak_pool_61_20260710_010112`): the 4 files above + defensively
`tools\proxy_buy_flow_selftest.py`, `tools\proxy_renew_flow_selftest.py` (those 2 were
backed up but ultimately NOT modified).

### storage.py
Block marker: `--- TPILOT PROXY POOL STAGE6.1 (renew notify dedupe + auto-renew toggle) START/END ---`
* New table (lazy `CREATE TABLE IF NOT EXISTS`, no import-time DB mutation):
  `proxy_renew_notify_log(lease_id INTEGER NOT NULL, notify_date TEXT NOT NULL,
  slot TEXT NOT NULL, sent_at TEXT NOT NULL, PRIMARY KEY(lease_id, notify_date, slot))`
* Helpers: `proxy_renew_notify_log_ready`, `proxy_renew_notify_mark_once` (returns True only
  the FIRST time per triple; PK conflict → False — this IS the dedupe mechanism),
  `proxy_renew_notify_purge_old`, `proxy_lease_set_auto_renew`.
* `auto_renew_enabled` column already existed in proxy_leases (Stage 2/3) — no ALTER added.

### main.py
* Slot constants (~line 29411): `_PRENEW_WARN_SLOTS` (tomorrow_noon 12:00, today_morning 9:00,
  today_day 13:00, today_evening 17:00), `_PRENEW_AUTORENEW_SLOTS` (autorenew_pre 12:00,
  autorenew_today_morning/day/evening 9/13/17), `_PRENEW_SLOT_WINDOW_MIN = 30`,
  `_PRENEW_LOOP_TICK_SEC = 600` (10-min tick < 60-min window → no slot ever skipped).
* `_prenew_notify_expiring_loop` REWRITTEN: Kyiv-local (`_kyiv_now().replace(tzinfo=None)`),
  slot-based, deduped via notify-log; grouped message for 2+ leases; single-lease message keeps
  the `lease_id: N` marker so PanelBot's `_prenew_notification_buttons` keep parsing it.
  Warnings NEVER write last_renew_attempt_at. The old 6h-cooldown `_prenew_cooldown_ok` was
  REMOVED — do not reintroduce.
* Pure gate functions (extracted specifically to be unit-testable without the loops):
  `_prenew_warn_eligible`, `_prenew_autorenew_gates_ok`. If you touch gating logic, keep it
  inside these pure functions.
* Shared spend executor `_prenew_execute_renewal(lease, *, source)` — THE only prolong_make
  `allow_spend=True` site. `_handle_proxy_renew_confirm_command` is now a thin wrapper around it.
  It deliberately does NOT check auto_renew_enabled (that gate belongs to the caller).
* Auto-renew: `_prenew_autorenew_one` (one attempt + exactly one admin notification),
  `_prenew_autorenew_loop` (gates → mark_once → attempt). Retry model: pre-day attempt, then
  up to 3 expiry-day attempts, one per slot, only if previous failed (dedupe is per slot).
* New command `/proxy_pool_autorenew <lease_id> <0|1>` → `_handle_proxy_pool_autorenew_command`
  (pure storage write). Routed in the Stage 5 `_panel_execute_command_text` override, which
  chains via `_PRENEW_PREV_PANEL_EXEC = globals().get("_panel_execute_command_text")`.
* Startup (CONTROLLER_MODE block, ~line 4824):
  `client.loop.create_task(_prenew_autorenew_loop())` added next to the existing
  `_prenew_notify_expiring_loop()` registration.
* `/proxy_pool_list` gained filter `available` = statuses (free, orphaned) —
  `_PPOOL_AVAILABLE_STATUSES`. `/proxy_pool_card` JSON gained `login` and `auto_renew_enabled`
  keys (still NEVER a raw `password` key — only `has_password` bool).

### panel_bot.py
* Stage 6.1A block marker: `--- TPILOT PROXY POOL STAGE6.1A ... START/END ---` (after
  `# --- TPILOT PROXY BUY STAGE4 END ---`): `_frompool_*` functions + standalone
  `_frompool_callback` (CallbackQuery). Onboarding button «🌐 Выбрать proxy из пула» added in
  `_add_manager_proxy_choice_buttons`. Picker uses `/proxy_pool_list available`; assign reuses
  `/proxy_pool_assign`; success sets wizard `("add_manager", "phone")` exactly like buy-success.
  One-time confirm token: `_frompool_confirm_state_ok` + `_wizard_clear` BEFORE spawning assign.
* Stage 6.1B: `_pbuy_result_text` / `_ppool_assign_result_text` headers conditional on
  `check_ok` («✅ Прокси куплен и настроен» vs «⚠️ Прокси куплен, но проверка не прошла»);
  `_ppool_guard_failed_buttons(lease_id, manager_key=None)` = check-again / sync / card /
  pick-another-from-pool.
* Stage 6.1C/F block marker: `--- TPILOT PROXY POOL STAGE6.1C (PIN password reveal) +
  STAGE6.1F (auto-renew toggle) ---` (right before `_ppool_callback`): `_ppool_pin_env_value`
  (PANEL_ADMIN_PASSWORD → fallback MANAGER_ADMIN_PASSWORD; empty → always deny),
  `_ppool_reveal_password_once` (direct `_connect_panel_db()` sqlite read — password NEVER
  enters panel_commands), `_ppool_run_autorenew_toggle`, standalone NewMessage handler
  `_ppool_reveal_pin_input` (wizard "ppool"/step "reveal_pin"; deletes the admin's PIN message;
  wrong PIN → deny; reveal message tells admin to delete it).
* Stage 6.1C card: `_ppool_card_text(data, *, details=False)` +
  `_ppool_card_buttons(filt, data, *, details=False)`; details toggle `ppool:details:<id>:<0|1>`;
  noisy fields (scheme/proxy_type, provider_order_number, last_renew_attempt) only in details mode.
* Stage 6.1D: `_ppool_edit_or_send(event, chat_id, text, buttons)` — edit-in-place with
  send-fallback; applied ONLY to list/card/check/sync screens. Buy/recover/renew results are
  deliberately NOT edited in place (own persisted messages). Nothing is ever deleted by this
  helper. The transient "⏳" pre-message in `_ppool_run_check` was removed.
* New callback branches in `_ppool_callback`: `ppool:details:`, `ppool:reveal:`,
  `ppool:autorenew:<id>:<0|1>`.
* Notifications: `_panel_notification_loop` dispatch (~line 4478) now handles kinds
  `proxy_renew_warning` → `_prenew_notification_buttons`, `proxy_autorenew_failed` →
  `_prenew_autorenew_failed_buttons` (renew-manually button routes to `renew:calc:` — the
  NO-SPEND preview, never directly to confirm). Kind `proxy_autorenew_ok` exists (success
  message, default buttons).
* `_submit_and_wait` timeout table: `/proxy_pool_autorenew` → 30s.

## 3. Validation already performed (2026-07-10, old PC) — expected to reproduce on new PC

* `python3.12 -m py_compile main.py panel_bot.py storage.py proxy_parser.py proxy_provider.py` → OK.
* All 6 selftests → `SELFTEST OK: all checks passed.`:
  proxy_pool (incl. ~30 Stage 6.1 checks), proxy_buy_flow, proxy_renew_flow, proxy_leases,
  proxy_parser, proxy_provider.
* AST audit: `allow_spend=True` as a real call keyword = EXACTLY 2 sites, both main.py
  (buy-confirm `make_ipv4` ~29284; `_prenew_execute_renewal` `prolong_make` ~29693).
  panel_bot.py/storage.py = 0.
* Mojibake scan (Ð / Ñ / â€) clean in all 4 changed files.
* Defense-in-depth confirmed: `_pbuy_provider()` always constructs
  `ProxySellerProvider(key, allow_spend=False)`; provider methods raise `SpendGuardError`
  by default.

## 4. Review findings — non-blocking, candidates for a future small patch

1. `proxy_renew_notify_purge_old` exists but is wired NOWHERE — notify-log grows unbounded
   (harmless volume). Future: call once daily from a loop.
2. PIN comparison is plain `!=` (not constant-time). Acceptable for an admin PIN; note only.
3. `auto_renew_enabled` has no standalone ALTER migration (only in CREATE TABLE). Fine for
   production (Stage 6 buy flow already writes it), relevant only for very old DBs.
4. Slot windows are ±30 min around fixed times; a controller outage covering a full 60-min
   window skips that slot (mitigated by 3 expiry-day slots + pre-day attempt).
5. Server env must have PANEL_ADMIN_PASSWORD (or MANAGER_ADMIN_PASSWORD) set, or password
   reveal always answers «PIN не настроен» (safe, but Ed should know).

## 5. Fragile areas, traps, and things that previously broke

* OVERRIDE STACKS (the #1 trap): main.py/panel_bot.py define the same function repeatedly;
  only the LAST def is active. `grep -n "def <name>"` → take the LAST hit. New overrides
  must capture the previous def (`_X_PREV = globals().get("name")`) and chain to it.
* CALLBACK DATA PARSING: with multi-parameter callbacks count colons carefully —
  e.g. `wiz:frompool:pick:{key}:{lease_id}` requires `split(":", 4)` so key and lease_id are
  SEPARATE elements. Off-by-one split limits silently merge trailing fields.
* PREFIX COLLISIONS: use exact `==` for a callback string that is a literal prefix of another
  (`ppool:sync` vs `ppool:sync_confirm`). All current prefixes verified non-colliding —
  selftest has a collision check; keep it updated when adding prefixes.
* ENCODING INCIDENT (real, happened during C3): PowerShell `Get-Content | Set-Content` turned
  Russian text into mojibake; panel_bot.py had to be restored from backup and re-patched via
  Python UTF-8. NEVER text-edit these files through PowerShell pipes.
* BOM: several project files start with a UTF-8 BOM. Analysis scripts must read with
  `encoding="utf-8-sig"` or `ast.parse` fails on U+FEFF.
* `_safe_event_edit` swallows MessageNotModifiedError but RE-RAISES other errors —
  `_ppool_edit_or_send` catches those and falls back to send. Keep that contract.
* Telethon handlers: the giant `panel_wizard_input` / `on_callback` dispatchers do NOT stop
  propagation — every registered handler sees every event. A new standalone handler is safe
  ONLY if its wizard name / callback prefix is not handled anywhere else (grep first).
  Wizard names in use by 6.1: "ppool" (steps list / reveal_pin), "frompool" (step confirm).
* Duplicate-click protection pattern: `_panel_callback_is_duplicate` (short window) PLUS a
  one-time wizard token consumed BEFORE spawning the task (see `_frompool_confirm_state_ok`,
  `_ppool_assign_confirm_state_ok`). Copy this pattern for any new confirm-then-act button.
* SELFTEST TECHNIQUE + LESSONS (bugs found during 6.1 — all in TEST code, none in production):
  - main.py/panel_bot.py are not importable → tests extract functions via
    `ast.parse` + filter by name + `ast.unparse` + `exec` into a namespace with fakes.
  - `ast.unparse` NORMALIZES QUOTE STYLE — never assert exact quoted substrings against
    unparsed source; use regex tolerant of both quote chars.
  - Extracted functions reference module globals — bind everything they use in `extra_ns`
    (missing `_PPOOL_STATUS_LABELS` and `normalize_manager_key` caused NameErrors).
  - Prefer extracting the REAL helper over faking it (a faked `_pbuy_safe_error` lambda gave
    false failures because the real one scrubs differently).

## 6. Deploy rules and the never-do list

Rules (in addition to CLAUDE.md §10):
* Stage 6.1 requires a FULL restart (`stop_everything.bat` → `start_everything.bat`) because
  main.py is shared by controller and all manager runtimes. `start_panel_bot.bat` alone is
  NOT sufficient for this patch.
* Deploy order is fixed: zip locally → transfer to server Desktop → server backup into
  `.backups\patch_<ts>` → Expand-Archive → py_compile (full 8-file list, with
  `.\venv\Scripts\python.exe`) → restart → process check → tail err-logs → Ed does a manual
  bot check (cards, buttons, one full pool screen round-trip).
* After deploy verify in logs that BOTH loops started and no tracebacks reference
  `prenew`/`ppool`/`frompool`.

NEVER:
* Never deploy a subset of the 3 files (they change together: command routing + UI + schema).
* Never make real Proxy-Seller API calls, buy or renew proxies during tests/diagnostics.
* Never add a third `allow_spend=True` call site; never set `allow_spend=True` on the
  provider CONSTRUCTOR.
* Never pass the proxy password through panel_commands / result_text / notifications / logs.
* Never run the bots locally on the dev PC (sanitized copy; sessions/DB are stale).
* Never edit files before creating the timestamped backup.
* Never use PowerShell Get-Content|Set-Content on Python files with Russian text.
* Never query/modify the production DB without an explicit request from Ed.

## 7. How to work with Ed, step by step

Ed (the owner) drives the process from Telegram + two PCs; Claude Code never deploys on its
own. Standard cycle for every task:

1. Ed sends a task prompt (contains Mode / Model / working dir / permissions / constraints).
   If any of those are missing — ask before acting.
2. Read-only diagnosis first (Ask mode). Deliver a numbered report. NO edits.
3. Ed reviews, then sends an implementation prompt (Accept edits). Create backups FIRST,
   implement in small compile-checked increments (py_compile after every file edit),
   run selftests, deliver the numbered report (backups / files / what / DB changes /
   tests / confirmations / risks).
4. Ed requests a final read-only review (Ask mode) — verify invariants, produce
   PASS/FAIL + blocking/non-blocking + zip list.
5. Only after Ed's explicit approval: give him the packaging command and the ONE server
   deploy block. Ed runs them manually and pastes outputs back.
6. Ed does the manual Telegram check; only then is the stage "deployed".

Communication: Russian, concise, «Что сделали / Что будем делать», every command labeled
with WHERE to run it (локально / на сервере). Do not start the next stage while the previous
one is un-deployed unless Ed says so.

## 8. Path translation from old materials

Any old chat logs, plans, or memory files may reference old-PC paths. Translate:

| Old reference | Meaning now |
|---|---|
| `C:\Users\annam\Desktop\ALM_TPilot` | new local dev dir `C:\Users\PROFESSOR\Desktop\ALM_TPilot` |
| `C:\Users\annam\.claude\plans\...` / `...\memory\...` | old-PC Claude files; content may be stale — verify against code |
| `C:\ALM_TPilot` | PRODUCTION SERVER — unchanged; never treat as local |
| `/c/Users/annam/AppData/.../python3.12` | old local interpreter; on new PC just use `python3.12` |
| host `workcar6`, `C:\Users\workcar6\...` | the server host (venv was built there) |

Rule of thumb: `C:\ALM_TPilot` in a command = run on server; anything under `Users\<name>\Desktop`
= local. If a command block mixes them, stop and re-check before giving it to Ed.

## 9. Questions the new Claude MUST ask before editing or deploying

Before FIRST edit on the new PC:
1. Was the project folder copied completely (incl. `tools\`, `.backups`-style bak files)?
   Run the read-only validation suite first and report.
2. Which CLAUDE.md is in the project root — the new one from CLAUDE_NEW_PC? If not, ask Ed
   to replace it before continuing.
3. Is `python3.12` available on this PC? (venv is non-functional by design.)

Before ANY deploy:
4. Has Stage 6.1 already been deployed from another machine? (Check with Ed; if uncertain,
   compare server file dates/markers before overwriting.)
5. Are there OTHER undeployed local changes on the server or old PC that this zip would
   overwrite (transfers-UX etc. — see §1 UNCERTAIN)? If unknown — ask Ed, do not guess.
6. Confirm the zip contents and the exact server backup step with Ed before he runs the block.
7. Is PANEL_ADMIN_PASSWORD set on the server (for the PIN reveal)? Ask Ed to confirm
   presence only — never the value.

Before touching any NEW area of code:
8. Ask whether a plan/diagnosis already exists for it (many plans predate this migration and
   may be partially implemented — verify markers in code first).
