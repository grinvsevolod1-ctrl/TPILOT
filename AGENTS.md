# TPilot project rules for Codex (transfer handoff, 2026-07-10)

This file is a full transfer of project rules + current state for continuing TPilot work
on a new computer. Save as `AGENTS.md` in the project root on the new PC.

## 1. Project identity and architecture

TPilot is a production Windows Python 3.12 / Telethon Telegram CRM/autofunnel project.

Components (all in one folder, one shared codebase):

* Controller (main.py in CONTROLLER_MODE) — orchestrates manager Telethon runtimes,
  executes panel commands from the `panel_commands` DB queue, runs background loops
  (lead dispatch, daily report, bizlink autocreate, proxy renewal warnings, proxy auto-renew, etc.).
* Manager runtimes (main.py per manager) — live Telethon client sessions that talk to leads.
* PanelBot (panel_bot.py) — admin Telegram bot UI. UI only: submits commands to
  `panel_commands` via `_submit_and_wait`; controller writes result back as JSON in `result_text`.
* panel_bridge.py — bridge between PanelBot and controller (incl. QR-login result scrubbing).
* ManagerBot (manager_bot.py) — manager-facing bot (stats, schedule, screenshots).
* PartnerBot (partner_stat_bot.py) — partner stats bot.
* storage.py — all SQLite access (central DB), aiosqlite, additive idempotent migrations.
* Single central SQLite DB; Kyiv timezone (`TZ_KYIV` / `_kyiv_now()`) for business logic.

## 2. Paths

**Never hardcode an absolute path in Python.** Use `tpilot_paths` (`ROOT`, `tpilot_db()`,
`require_db()`, `manager_db_paths()`, `manager_runtime_dir()`), which derives everything
from the tree the file lives in and honours the `TPILOT_ROOT` / `TPILOT_DB_PATH`
overrides. A hardcoded path that "prefers the server and falls back to local" is a bug,
not a convenience: running such a script from a checkout silently reads or writes the
PRODUCTION database.

* Ubuntu deployment root: `/opt/tpilot` (installer default, `--dir` overrides)
* Deployment DB: `<root>/db/data_tpilot.db`
* Legacy Windows paths (`C:\ALM_TPilot`, `C:\Users\...\ALM_TPilot`) are historical only.

A dev checkout is a **sanitized copy**, not the live system:

* `.env.TPilot` may hold redacted/empty token values — verify before assuming
  misconfiguration.
* A `venv/` copied from another host does NOT run; build it locally
  (`python3.12 -m venv venv && ./venv/bin/pip install -r requirements.txt`).
* The DB and `.session` files in a checkout are stale copies. A sanitized copy has a
  REDUCED schema (~17 tables, no `managers`), so scripts may legitimately report missing
  tables — that is the copy, not a defect.

The server tree is **not** a git repository. Safety there = timestamped file backups
(`<file>.py.bak_<patch>_<YYYYMMDD_HHMMSS>` created BEFORE editing), py_compile,
selftests, explicit validation reports.

## 3. Main files

Active code:

* main.py — controller + manager runtime + all `/command` handlers (`_panel_execute_command_text`).
* storage.py — DB layer (managers, leads, schedules, bizlinks, proxy_leases, panel queue...).
* panel_bot.py — AdminBot UI (menus, wizards, callback handlers, notification loop).
* panel_bridge.py — panel/controller bridge.
* manager_bot.py — ManagerBot (stats, work-day schedule toggles, M1 screenshots).
* partner_stat_bot.py — PartnerBot stats.
* router.py, profile_extractor.py, profile_dialog.py, profile_texts.py,
  post_followup_texts.py, texts.py — lead parsing/dialog/texts.
* manager_registry.py — manager registry helpers (`normalize_manager_key`, paths).
* stats_engine.py, stats_parity_harness.py — shared stats logic (PartnerBot + ManagerBot).
* health_server.py, soft_watchdog_pinger.py — health/watchdog.
* proxy_provider.py — Proxy-Seller API client with spend-guard (`allow_spend=False` default,
  raises `SpendGuardError`).
* proxy_parser.py — proxy string parsing/masking.
* tools/*_selftest.py — offline selftests (temp SQLite + fake providers, no network).

Platform/ops layer (Ubuntu migration, Stages 1–2):

* process_control.py — THE single source of truth for "is this process alive": process
  listing (psutil → /proc → `ps -eo ... -ww`), manager-key extraction from argv, verified
  stop (systemd → SIGTERM → SIGKILL, always re-checked), start. Four separate consumers
  used to answer this question differently; never reintroduce a local copy of this logic.
* manager_launcher.py — manager startup with the session-isolation contract (see §6) and
  the `start_status.json` result protocol.
* tpilot_ctl.py — the operator CLI (`tpilot-ctl`), replaces all 30 deleted `.bat`/`.ps1`
  scripts. Delegates to systemd when present, falls back to direct process control.
* tpilot_paths.py — path resolution; see §2.
* deploy/ — `install_ubuntu.sh` (idempotent installer) and `systemd/` (8 units +
  `tpilot.target`, with `tpilot-manager@.service` templated per manager key).

Backups/old copies are NOT active code (main_beka03.06.py, *_cursor_copy.py, *.bak_*).
Never edit or run them unless explicitly asked.

## 4. Critical architecture warnings

* main.py and panel_bot.py contain MANY stacked override definitions of the same function.
  Only the LAST definition is active. Before editing, always locate the active/latest def
  by line number (`grep -n "def name"` → take the last). Never assume the first def is active.
* New overrides follow the chain pattern: `_X_PREV = globals().get("name")` before redefining,
  delegate to `_X_PREV` as fallback.
* panel_bot.py callback handler order matters; for callback data where one string is a prefix
  of another (e.g. `ppool:sync` vs `ppool:sync_confirm`) exact `==` matching is used —
  preserve it.
* main.py/panel_bot.py cannot be imported standalone (Telethon/env side effects at import).
  Selftests use AST extraction (`ast.parse` + `ast.unparse` + `exec`) — follow the same
  technique in tools/ when adding tests.
* Avoid broad refactors. Minimal targeted patches only.

## 5. Current working state (as of 2026-07-10)

Deployed and working on server:

* QR login for managers (segno==1.6.6 on server; QR token never logged; panel_bridge scrubs QR_URL).
* Business links tg_limit classifier/retry (CHATLINKS_TOO_MUCH checked before no_business).
* Reserve accounts (activation issues 15 links; activity in stats on activation date).
* Stats engine wiring for PartnerBot + ManagerBot (AdminBot runtime-stats stage was still
  pending; schedule_aware disabled globally).
* ManagerBot M1 screenshots workflow.
* C1 source work-days inheritance, C2 source greeting/away time inheritance,
  C3 manager future schedule requests (all deployed).
* Manager auth fix 2026-06-22: stable Telethon device fingerprint (`_TELETHON_DEVICE_KWARGS`),
  optional `ONBOARD_API_ID`/`ONBOARD_API_HASH` env pair.
* Proxy pool Stage 6 (P1–P4): proxy_leases table, pool screens in PanelBot
  (list/card/check/sync/assign/unassign), buy flow with spend-guard, recover flow.
* Proxy renew Stage 5: renew calc/confirm/defer commands + warning notifications.

**Implemented locally, reviewed (PASS), NOT YET DEPLOYED: Proxy Pool Stage 6.1 (A–F).**
Deploy package = `main.py`, `panel_bot.py`, `storage.py` (selftest file optional, not needed at runtime).
Backups on old PC: `*.bak_pool_61_20260710_010112`.

Stage 6.1 contents:

* A — onboarding "🌐 Выбрать proxy из пула" picker (free/orphaned only, no spend,
  success continues to phone step).
* B — buy/assign result wording conditional on `check_ok` + recovery buttons on failed guard.
* C — clean proxy card + "🔧 Детали" toggle + PIN-protected password reveal
  (`ppool:reveal:` → PIN text input → direct local DB read in panel_bot.py,
  NEVER through panel_commands; PIN env = PANEL_ADMIN_PASSWORD, fallback MANAGER_ADMIN_PASSWORD;
  missing PIN env → deny "PIN не настроен").
* D — pool screens edit-in-place (`_ppool_edit_or_send`); buy/renew results stay
  as their own persisted messages (never deleted).
* E — renewal warning dedupe: table `proxy_renew_notify_log(lease_id, notify_date, slot,
  sent_at, PK(lease_id,notify_date,slot))` + `proxy_renew_notify_mark_once`; Kyiv slots
  tomorrow_noon / today_morning / today_day / today_evening; grouped message for 2+ leases;
  warnings never write last_renew_attempt_at.
* F — per-proxy auto-renew toggle (`ppool:autorenew:` → `/proxy_pool_autorenew <id> <0|1>`);
  shared executor `_prenew_execute_renewal` in main.py; auto-renew loop with slots
  autorenew_pre / autorenew_today_* and full gate set (status active, auto_renew_enabled==1,
  provider_type proxy_seller, provider_proxy_id present, period_id resolved, provider
  configured, per-slot dedupe).

## 6. Hard safety invariants (verify after ANY edit to these areas)

* `allow_spend=True` appears as a real call argument in EXACTLY 2 places project-wide,
  both in main.py: buy-confirm `make_ipv4` (in `_handle_manager_proxy_buy_confirm_command`)
  and `prolong_make` inside `_prenew_execute_renewal` (~lines 29284/29693, will shift).
  Audit with AST or regex after every change; selftest check 30 enforces this.
* `_pbuy_provider()` always constructs `ProxySellerProvider(..., allow_spend=False)`.
* Raw proxy password never enters panel_commands/result_text/JSON/list/card — only
  `has_password` boolean; reveal is panel-side direct sqlite read after correct PIN.
* Passwords/API keys never logged or included in error texts (`_pbuy_safe_error` scrubs).
* check/sync/assign/unassign/choose-from-pool flows never call spend methods.
* First client message is never profile evidence (first-message rule). Passive parsing
  must always work; greeting/questionnaire settings must not block it.
* Night leads/dolyoty window 17:00–08:00 only for stat modes needing it; normal stat
  modes use calendar day 00:00–00:00. C2 affects greeting/away timing, not stats/dolyoty.

## 7. Unresolved issues / risks / fragile areas

* Stage 6.1 not deployed yet — deploy is the next step.
* Non-blocking review notes for a future patch: `proxy_renew_notify_purge_old` exists but
  is not wired anywhere (notify-log grows unbounded, harmless); PIN comparison is not
  constant-time; `auto_renew_enabled` exists only in CREATE TABLE (fine because the deployed
  Stage 6 buy flow already writes it — relevant only for very old DBs).
* Verify `PANEL_ADMIN_PASSWORD` (or MANAGER_ADMIN_PASSWORD) is set on the server, otherwise
  password reveal always denies (safe but unexpected).
* AdminBot runtime-stats stage: pending (uncertain — verify current state on server).
* Planned but unconfirmed (uncertain whether implemented/deployed): bulk "Ссылки на дату"
  wizard (bld: namespace) + buyer-request push-card buttons; pending undeployed transfers-UX
  changes in panel_bot.py/manager_bot.py/storage.py were mentioned in that plan — verify
  against server before building on top.
* Permanent risks: override traps, mojibake from PowerShell text edits, no git, Windows
  PowerShell syntax differences, production DB/session protection, Telethon live-session
  risks, deployment/restart risk, stale old-copy files.

## 8. Codex workflow (required)

* Diagnostics/review: Mode **Ask permissions**, read-only. Owner sends output back.
* Implementation: Mode **Accept edits**.
* Risky/cross-file planning: **Plan mode**.
* Final review before packaging: Mode **Ask**, read-only.
* No partial deployment — deploy only when the whole block (all sub-stages) is implemented,
  validated and reviewed.
* Avoid Auto mode / Bypass permissions unless owner explicitly approves.
* Plan screens: read-only audit → owner Rejects after report; approved implementation →
  Accept; corrections → Revise with revision text only.
* Model choice per task is set by the owner in the prompt header (historically:
  Opus for diagnosis/review, Sonnet for implementation).

Per-task sequence: 1) read-only diagnosis → 2) owner sends output → 3) implementation
prompt → 4) implementation → 5) owner sends output → 6) review → 7) package changed
files only → 8) deploy with server backup → 9) restart + logs → 10) manual bot check.

## 9. Validation before any deploy

```
cd /path/to/checkout
./venv/bin/python -m py_compile main.py panel_bot.py panel_bridge.py storage.py \
  manager_registry.py stats_engine.py partner_stat_bot.py manager_bot.py \
  process_control.py preflight_check.py soft_watchdog_pinger.py health_server.py \
  manager_launcher.py tpilot_ctl.py tpilot_paths.py
```

For proxy work also run the offline selftests (temp SQLite + fake providers, no network,
no spend):

```
for t in tools/proxy_*_selftest.py; do ./venv/bin/python "$t"; done
```

For any change to process control, startup, or the deploy layer:

```
./venv/bin/python tools/process_control_selftest.py
./venv/bin/python tools/manager_launcher_selftest.py
bash -n deploy/install_ubuntu.sh && bash deploy/install_ubuntu.sh --dry-run
```

Always also: mojibake scan (search `Ð`, `Ñ`, `â€`) in changed files; allow_spend audit;
show diff/summary before owner approval.

### Selftest harness (Stage 3 — all 10 formerly failing selftests now pass)

The 10 long-standing failures were STALE TEST HARNESSES, not product bugs: the product
code had been refactored correctly and the AST-extraction harnesses were never updated.
All are fixed; the causes are recorded because they will recur on the next refactor.

**`tools/ast_extract.py` is now the shared AST harness. Use it — do not write a new
private copy.** 102 of 154 selftests still carry their own copy; migrate opportunistically
when touching one. It handles the three shapes a private copy always gets wrong:

1. **Module-level alias, not a `def`.** When a helper moves into a module and is re-bound
   (`_manager_label_from_row = text_format_helpers.manager_label_from_row`), a def-only
   extractor reports "could not find ...". Affected `deleted_manager_stats_retention`,
   `proxy_buy_flow`.
2. **Un-injected module in the exec namespace.** Extracted `main.py` code reaches modules
   through module-level `import` aliases, which name-based extraction never captures →
   `NameError: text_format_helpers / proxy_parser`. `safe_module_ns()` seeds them all;
   splat it FIRST so explicit fakes still win. Affected `manager_relogin`,
   `manager_replacement_{adminbot,backend,commit}`. (`identity_sync_ira` was the same
   shape but a real `storage._db_conn` dependency, bound explicitly.)
3. **Last-wins.** `extract_nodes` takes the LAST top-level definition, matching Python and
   the override convention in §4. Private copies that collect every occurrence break on
   any duplicated name. Accepts `str` or `Path`.

Ground truth for `liquid_ru_locations_parity` / `non_liquid_locations_parity` (267k
records) is recovered from **git history** — commit 5e13c32 untracked the
`*.bak_datamove_*` artifacts on purpose, but the blobs remain reachable. Do NOT re-add
those 20 MB files to the working tree.

**`prepared_accounts_offline_import`: 17 failures, one root cause — and a live hazard.**
`tdata_import/session_inspector.py` hardcoded schema `7` (telethon==1.42.0). Telethon
1.44 writes schema **8**, so on any host resolving a newer Telethon EVERY prepared-account
import fails with "unsupported schema version 8 (need 7)" — a message that blames the
operator's session file, not the dependency. The constant is now derived from the
installed Telethon (fallback 7), and the comparison stays EXACT on purpose: accepting
">= 7" would let an unknown future schema into session installation, where a wrong
install is unrecoverable without re-login.

The real danger is on disk: **Telethon migrates an older `.session` IN PLACE on first
open** (`SQLiteSession.__init__` → `_upgrade_database` + `save`). All live sessions are
schema 7, so a Telethon upgrade is a one-way migration of live credentials, recoverable
only from a backup. `tpilot-ctl doctor` now names every stale session and warns before
managers start; `install_ubuntu.sh` reports the same agreement at install time. **Back up
`runtime/managers/*/` before any Telethon upgrade.**

### Override chains: navigate, do not "clean up"

`main.py` has 63 duplicated top-level names (139 shadowed defs, worst:
`_panel_execute_command_text` ×35); `panel_bot.py` has 35 (65 shadowed). These are NOT
dead code — every duplicated name participates in delegation (325 `globals().get()`
captures in `main.py`), so a shadowed def is still reachable as a fallback and deleting
one silently drops behaviour.

- `python tools/override_map.py <name>` — every definition, which one is ACTIVE, and the
  delegation captures. Use this instead of `grep -n "def name" | tail -1`.
- `python tools/override_map.py --stats` — whole-file picture.
- `tools/override_chain_selftest.py` pins the counts, so a "duplicate cleanup" that drops
  a shadowed def fails loudly (mutation-verified). Update the baseline only with a
  deliberate, explained change.

## 10. Deployment process

First-time install on a new Ubuntu host:

```
sudo bash deploy/install_ubuntu.sh --dry-run    # review the plan first
sudo bash deploy/install_ubuntu.sh
```

The installer is idempotent and never overwrites `.env.TPilot`, the DB, or `.session`
files. It creates the `tpilot` service user, the venv, 8 systemd units, and the
`tpilot-ctl` command.

Updating an existing deployment:

```
cd /opt/tpilot
sudo -u tpilot git pull                     # or rsync the changed files
sudo -u tpilot ./venv/bin/python -m py_compile <changed files>   # see §9
sudo tpilot-ctl restart-all
```

Checks after restart:

```
tpilot-ctl status
tpilot-ctl doctor
journalctl -u tpilot-panel-bot -n 40
journalctl -u tpilot-manager@<key> -n 40 -f
```

If only PanelBot changed: `sudo systemctl restart tpilot-panel-bot`.

Rollback: systemd keeps the previous unit files, but code rollback is still manual —
take a timestamped copy of changed files before overwriting them (§2 backup rule still
applies; there is no git history on the server).

NOTE: the old Windows rule "duplicate Python process pairs are normal (venv launcher +
AppData child), do not kill the child separately" is Windows-only and is enforced in code
by `process_control.collapse_launcher_children`. On Linux a manager process is NOT a
launcher pair, and every listed manager PID is real.

## 11. Protected files and folders

Never edit or print secrets from: `.env`, `.env.TPilot`, `*.session`, `sessions/`, `db/`,
`runtime/`, `logs/`, `config/`, `exports/`, `venv/`, `__pycache__/`.

* Do not query production DB unless explicitly asked.
* Do not run live Telegram sessions or sends during diagnostics.
* Do not run main.py / panel_bot.py / manager_bot.py / partner_stat_bot.py directly
  unless explicitly asked.
* Never edit production server files without backup + explicit deployment step.
* Never make real Proxy-Seller API calls, buy or renew real proxies in tests —
  temp SQLite + fake providers only.

## 12. Windows command style + encoding

* Short familiar PowerShell/BAT commands. No Bash heredoc on Windows. No long scripts
  unless absolutely needed.
* Server scripts: `stop_everything.bat`, `start_everything.bat`, `restart_everything.bat`,
  `start_manager_bot.bat`, `start_panel_bot.bat`.
* NEVER edit Python files with Russian text via `Get-Content | Set-Content` — it creates
  mojibake. Use Python with explicit UTF-8:

```
@'
from pathlib import Path
p = Path("panel_bot.py")
text = p.read_text(encoding="utf-8")
text = text.replace("old", "new")
p.write_text(text, encoding="utf-8")
'@ | python3.12 -
```

* After any text edit, scan for mojibake patterns: `Ð`, `Ñ`, `â€`.
* Note: some project files start with a UTF-8 BOM — read with `encoding="utf-8-sig"`
  in analysis scripts.

## 13. Communication with owner

* Respond in Russian. Concise, professional, practical, cautious.
* Always structure: **Что сделали** / **Что будем делать**.
* For every command: say exactly WHERE to run it (local PC vs server).
* For every Codex prompt: specify Mode, Model, Working directory.

## 14. How future prompts must be structured

Every task prompt to Codex contains:

1. Working directory.
2. Mode (Ask / Accept edits / Plan) + Model.
3. Permissions: exact list of files allowed to edit.
4. Hard constraints (no deploy, no real API calls, no .env/DB/session/log edits, backups first).
5. Task description with exact expected behavior per sub-stage.
6. Required tests/validation commands.
7. Expected output format (numbered report: backups, files changed, what was implemented,
   DB changes, test results, confirmations, risks).

Backup naming: `filename.py.bak_<patch>_<YYYYMMDD_HHMMSS>`, created BEFORE editing.
