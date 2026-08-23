# TPilot project rules (rewritten 2026-08-23)

Single source of truth for working on TPilot. Supersedes and replaces the old
AGENTS.md (2026-07-10 handoff), CLAUDE.md, HANDOFF.md, ADDENDUM_FOR_NEW_PC.md,
and NEXT_STEPS.md — all deleted; their history lives in git.

Key policy change (owner decision, 2026-08-23): **full refactoring is now
allowed and desired** (see section 5). The old "minimal targeted patches only"
rule is revoked. Product safety invariants (section 4) remain absolute.

## 1. Project identity and architecture

TPilot is a production Python 3.12 / Telethon Telegram CRM/autofunnel project.
Target platform: Ubuntu (`/opt/tpilot`), managed by systemd, developed via git
(repo: grinvsevolod1-ctrl/TPILOT). The Windows era is over: no `.bat`/`.ps1`
scripts, no file-copy backups as the primary safety net — git is the source of
truth.

Components (one shared codebase):

* Controller (main.py in CONTROLLER_MODE) — orchestrates manager Telethon
  runtimes, executes panel commands from the `panel_commands` DB queue, runs
  background loops (lead dispatch, daily report, bizlink autocreate, proxy
  renewal warnings, proxy auto-renew, notify-log purge, etc.).
* Manager runtimes (main.py per manager) — live Telethon client sessions that
  talk to leads.
* PanelBot (panel_bot.py) — admin Telegram bot UI. UI only: submits commands to
  `panel_commands` via `_submit_and_wait`; controller writes result back as
  JSON in `result_text`. One deliberate exception: PIN-protected proxy password
  reveal reads local sqlite directly (never through panel_commands).
* panel_bridge.py — bridge between PanelBot and controller (incl. QR-login
  result scrubbing).
* ManagerBot (manager_bot.py) — manager-facing bot (stats, schedule toggles,
  M1 screenshots).
* PartnerBot (partner_stat_bot.py) — partner stats bot.
* storage.py — all SQLite access (central DB), aiosqlite, additive idempotent
  migrations.
* Single central SQLite DB; Kyiv timezone (`TZ_KYIV` / `_kyiv_now()`) for
  business logic.

## 2. Paths and environments

**Never hardcode an absolute path in Python.** Use `tpilot_paths` (`ROOT`,
`tpilot_db()`, `require_db()`, `manager_db_paths()`, `manager_runtime_dir()`),
which derives everything from the tree the file lives in and honours the
`TPILOT_ROOT` / `TPILOT_DB_PATH` overrides. A hardcoded path that "prefers the
server and falls back to local" is a bug: run from a checkout, it silently
touches the PRODUCTION database.

* Ubuntu deployment root: `/opt/tpilot` (installer default, `--dir` overrides)
* Deployment DB: `<root>/db/data_tpilot.db`
* Legacy Windows paths (`C:\ALM_TPilot`, ...) are historical only.

A dev checkout is a sanitized copy, not the live system:

* `.env.TPilot` may hold redacted/empty token values — verify before assuming
  misconfiguration.
* The DB and `.session` files in a checkout are stale copies; a sanitized copy
  has a REDUCED schema, so scripts may legitimately report missing tables.
* Build the venv locally:
  `python3.12 -m venv venv && ./venv/bin/pip install -r requirements.txt`

## 3. Main files

Active code:

* main.py — controller + manager runtime + all `/command` handlers
  (`_panel_execute_command_text`). ~31K lines; refactoring target (R2).
* storage.py — DB layer (managers, leads, schedules, bizlinks, proxy_leases,
  panel queue...).
* panel_bot.py — AdminBot UI (menus, wizards, callback handlers, notification
  loop). ~23K lines; refactoring target (R3).
* panel_bridge.py, manager_bot.py, partner_stat_bot.py.
* router.py, profile_extractor.py, profile_dialog.py, profile_texts.py,
  post_followup_texts.py, texts.py — lead parsing/dialog/texts.
* manager_registry.py — manager registry helpers.
* stats_engine.py, stats_parity_harness.py — shared stats logic.
* health_server.py, soft_watchdog_pinger.py — health/watchdog.
* proxy_provider.py — Proxy-Seller API client with spend-guard
  (`allow_spend=False` default, raises `SpendGuardError`).
* proxy_parser.py — proxy string parsing/masking.
* tools/*_selftest.py — offline selftests (temp SQLite + fake providers,
  no network). 169 files, all passing as of 2026-08-23.
* tools/override_map.py — reports every definition of a name, marks the ACTIVE
  one, lists delegation captures.
* tools/ast_extract.py — the shared AST extraction harness for selftests.

Platform/ops layer (Ubuntu):

* process_control.py — THE single source of truth for "is this process alive":
  process listing (psutil → /proc → `ps`), manager-key extraction from argv,
  verified stop (systemd → SIGTERM → SIGKILL, always re-checked), start.
  Never reintroduce a local copy of this logic.
* manager_launcher.py — manager startup with the session-isolation contract
  and the `start_status.json` result protocol.
* tpilot_ctl.py — the operator CLI (`tpilot-ctl`). Delegates to systemd when
  present, falls back to direct process control.
* tpilot_paths.py — path resolution; see section 2.
* deploy/ — `install_ubuntu.sh` (idempotent installer) and `systemd/`
  (8 units + `tpilot.target`, `tpilot-manager@.service` templated per manager).

Backup/old-copy files (`*.bak_*`, `*_cursor_copy.py`, `main_beka*.py`) are NOT
active code and are scheduled for deletion in refactoring Stage R0.

## 4. Hard safety invariants (absolute — verify after ANY edit to these areas)

These protect real money and live credentials. They constrain BEHAVIOR, never
structure: code may move freely between files as long as these hold.

* `allow_spend=True` appears as a real call argument in EXACTLY 2 places
  project-wide: buy-confirm `make_ipv4` (manager proxy buy confirm) and
  `prolong_make` inside the renewal executor (`_prenew_execute_renewal`).
  Both currently live in main.py; after extraction (R2) they may live in the
  proxy module — the COUNT stays 2. Audit with AST or regex after every change;
  selftest check 30 enforces this.
* `_pbuy_provider()` always constructs `ProxySellerProvider(..., allow_spend=False)`.
* Raw proxy password never enters panel_commands/result_text/JSON/list/card —
  only a `has_password` boolean. Reveal is panel-side direct sqlite read after
  a correct PIN, compared with `hmac.compare_digest` (constant-time; keep it).
* Passwords/API keys never logged or included in error texts
  (`_pbuy_safe_error` scrubs).
* check/sync/assign/unassign/choose-from-pool flows never call spend methods.
* QR login token never logged; panel_bridge scrubs QR_URL from results.
* First client message is never profile evidence (first-message rule). Passive
  parsing must always work; greeting/questionnaire settings must not block it.
* Night leads/dolyoty window 17:00–08:00 only for stat modes needing it; normal
  stat modes use calendar day 00:00–00:00. C2 affects greeting/away timing,
  not stats/dolyoty.
* Session installation never guesses: `SESSION_SCHEMA_VERSIONS` in
  `tdata_import/session_inspector.py` is an EXACT allow-set (`{7, 8}`), never a
  `>=` range and never derived from the installed Telethon. Add a version only
  after confirming the `sessions` row layout is unchanged. Telethon migrates
  older `.session` files IN PLACE on first open — back up `runtime/managers/*/`
  before any Telethon upgrade.

## 5. Refactoring policy — ALLOWED (owner decision, 2026-08-23)

Full refactoring is permitted and desired: splitting main.py and panel_bot.py
into modules, collapsing override chains, deleting dead code, adding CI gates.
What replaces the old prohibition is a discipline, not a ban:

### Non-negotiable gates for EVERY refactoring step

1. **Full selftest suite passes** (all tools/*_selftest.py) before merging.
   A step that cannot be validated by selftests gets a selftest first.
2. **Invariant audit passes** (section 4): `allow_spend` count audit +
   password-leak grep after every change in proxy/panel areas.
3. **Behavior-preserving by default.** Refactoring commits change structure,
   not behavior. Behavior changes ship as separate commits with their own tests.
4. **One concern per commit**; work on feature branches; merge to the default
   branch only when green.
5. **Run `python tools/override_map.py <name>` before touching any function**
   in main.py / panel_bot.py — only the LAST definition is active, and shadowed
   defs are still reachable through `globals().get()` delegation captures
   (325 in main.py). When collapsing a chain, preserve the full delegation
   semantics of the ACTIVE body, then delete the shadowed defs it no longer
   references — never delete first.
6. **`tools/override_chain_selftest.py` pins the override counts.** Every
   collapse batch must update its baseline deliberately, in the same commit,
   with an explanation.
7. **Mojibake scan after every edit of files with Russian text**: check for
   `Ð`, `Ñ`, `â€` AND for U+FFFD
   (`python -c "import sys; sys.exit('\ufffd' in open(sys.argv[1],encoding='utf-8').read())" <file>`).
   Some files start with a UTF-8 BOM — read with `encoding="utf-8-sig"` in
   analysis scripts.

### Roadmap (execute in order; each stage independently shippable)

* **R0 — Dead weight removal.** Delete backup/old-copy files (`*.bak_*`,
  `*_cursor_copy.py`, `main_beka*.py`, stale zips/logs), extend .gitignore.
  Zero code changes. Do NOT re-add the 20 MB `*.bak_datamove_*` artifacts —
  the parity-test ground truth is recovered from git history (commit 5e13c32).
* **R1 — Collapse override chains.** main.py: 63 duplicated top-level names
  (139 shadowed defs, worst `_panel_execute_command_text` ×35); panel_bot.py:
  35 (65 shadowed). Small batches (5–15 names), full selftest run + baseline
  update per batch, per the gates above.
* **R2 — Extract subsystems from main.py** into importable modules, one at a
  time (suggested order: proxy pool/renew executor → bizlinks → lead dispatch →
  daily report → panel command executor). main.py imports them. After each
  extraction: selftests + invariant audit (the `allow_spend` count follows the
  code to its new module).
* **R3 — Split panel_bot.py by screen/namespace** (ppool, bld, onboarding,
  stats screens, notification loop) into a `panel/` package. Preserve exact
  callback-data matching semantics: where one callback string is a prefix of
  another (e.g. `ppool:sync` vs `ppool:sync_confirm`), exact `==` matching is
  used — keep it.
* **R4 — Import hygiene.** Make modules importable without Telethon/env side
  effects at import time (client construction under factories / `__main__`),
  so selftests for extracted code use direct imports instead of AST extraction.
* **R5 — Static gates / CI.** A single `tools/ci_check.py` running py_compile,
  pyflakes/ruff, the invariant audit, the mojibake scan, and the selftest
  suite; wire it into GitHub Actions and the deploy procedure.

### Selftest technique during the transition

Until R4 lands for a given module, main.py/panel_bot.py cannot be imported
standalone. Existing selftests use AST extraction — **use the shared
`tools/ast_extract.py`, never a new private copy.** It handles the four shapes
private copies get wrong: module-level aliases (not `def`s), un-injected module
namespaces (`safe_module_ns()`, splat FIRST so explicit fakes win), helper
seams via the `_AUTO_HELPERS` allow-list (pure functions only; when a test
controls time, bind `_tp_utc_now` to its clock with a NON-raising stub), and
last-wins duplicate resolution. ~100 older selftests still carry a private
copy — migrate opportunistically when touching one. Extracted modules (R2/R3)
must be plainly importable and tested by direct import.

## 6. Current state (as of 2026-08-23)

* All product stages are in this repository and deployed or ready: QR login,
  bizlink tg_limit classifier/retry, reserve accounts, stats engine wiring,
  M1 screenshots, C1–C3 inheritance/schedule features, proxy pool Stage 6 and
  6.1 (A–F), proxy renew Stage 5.
* Post-6.1 fixes applied and pushed: `proxy_renew_notify_purge_old` wired into
  the controller loop (notify-log no longer grows unbounded); PIN comparison
  uses `hmac.compare_digest`.
* Full diagnostic run 2026-08-23: 169/169 selftests PASS; pyflakes clean on
  all active modules; invariant audit clean. Two tests
  (`bizlink_readiness_integration`, `tg_health_recovery`) are slow and may
  flake on timeouts under parallel runs — rerun individually before treating
  as failures.
* Operational check: verify `PANEL_ADMIN_PASSWORD` (fallback
  `MANAGER_ADMIN_PASSWORD`) is set on the server, otherwise password reveal
  always denies (safe but unexpected).

## 7. Workflow

Git is the source of truth. The old no-git, file-backup, mode-per-prompt
workflow is retired.

* Work on feature branches; merge to the default branch only when the gates in
  section 5 are green. Direct pushes to the default branch require explicit
  owner approval.
* Owner pre-approval is required only for: production deploys, schema
  migrations that drop/rewrite data, anything touching the spend paths
  (section 4), and Telethon version upgrades (in-place session migration risk).
* Never make real Proxy-Seller API calls, buy or renew real proxies in tests —
  temp SQLite + fake providers only. Never run live Telegram sessions or sends
  during diagnostics.
* Do not run main.py / panel_bot.py / manager_bot.py / partner_stat_bot.py
  directly unless explicitly asked.
* Protected files — never edit or print secrets from: `.env`, `.env.TPilot`,
  `*.session`, `sessions/`, `db/`, `runtime/`, `logs/`, `config/`, `exports/`,
  `venv/`, `__pycache__/`. Production DB and `.session` files are never
  committed and never edited by hand.
* Communication with owner: respond in Russian; concise, professional,
  practical. Structure: **Что сделали** / **Что будем делать**. For every
  command, say exactly WHERE to run it (local checkout vs server).

## 8. Validation before any merge or deploy

```
./venv/bin/python -m py_compile main.py panel_bot.py panel_bridge.py storage.py \
  manager_registry.py stats_engine.py partner_stat_bot.py manager_bot.py \
  process_control.py preflight_check.py soft_watchdog_pinger.py health_server.py \
  manager_launcher.py tpilot_ctl.py tpilot_paths.py
```

Selftests: run the full suite for refactoring stages; for targeted feature
work, at minimum the suites covering the touched area, e.g.:

```
for t in tools/proxy_*_selftest.py; do ./venv/bin/python "$t"; done
./venv/bin/python tools/process_control_selftest.py
./venv/bin/python tools/manager_launcher_selftest.py
bash -n deploy/install_ubuntu.sh && bash deploy/install_ubuntu.sh --dry-run
```

Always also: mojibake scan (both classes, section 5 gate 7) in changed files;
`allow_spend` audit; diff/summary before owner approval of protected changes.

## 9. Deployment (Ubuntu)

First-time install on a new host:

```
sudo bash deploy/install_ubuntu.sh --dry-run    # review the plan first
sudo bash deploy/install_ubuntu.sh
```

The installer is idempotent and never overwrites `.env.TPilot`, the DB, or
`.session` files. It creates the `tpilot` service user, the venv, 8 systemd
units, and the `tpilot-ctl` command.

Updating an existing deployment:

```
cd /opt/tpilot
sudo -u tpilot git pull            # deploy from a known-good commit/tag
sudo -u tpilot ./venv/bin/python -m py_compile <changed files>
sudo tpilot-ctl restart-all
```

Checks after restart:

```
tpilot-ctl status
tpilot-ctl doctor                  # also names stale sessions before a Telethon upgrade
journalctl -u tpilot-panel -n 40
journalctl -u tpilot-manager@<key> -n 40 -f
```

If only PanelBot changed: `sudo systemctl restart tpilot-panel`
(`tpilot-manager-bot.service` is the separate ManagerBot).

Rollback: `git checkout <previous-good-commit>` on the server tree + restart.
Take a DB backup before schema-affecting releases; back up
`runtime/managers/*/` before any Telethon upgrade (one-way in-place `.session`
migration).
