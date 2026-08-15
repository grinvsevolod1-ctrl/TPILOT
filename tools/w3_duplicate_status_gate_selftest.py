# -*- coding: utf-8 -*-
"""tools/w3_duplicate_status_gate_selftest.py -- offline self-test for the W3.1 pure
duplicate-status eligibility gate (storage.w3_duplicate_status_eligibility /
storage.w3_countable), against the frozen contract in
16_DUPLICATE_STATUS_OWNER_ADDENDUM.md.

W3.1 only DEFINES the gate (no writer/reader/report calls it yet -- that is wave
W3.5-A/W3.5-B), so every test here calls the pure function directly with a synthetic
lead_row dict. No database, no network, no Telegram. Test IDs below map 1:1 onto both
numbering schemes in the source documents:
  - the W3.1 task spec's own "DUPLICATE GATE TEST MATRIX" items 1-16 (section 4)
  - the frozen Plan Freeze tier tests J14-J22 (16_DUPLICATE_STATUS_OWNER_ADDENDUM.md 16.8)

2026-07-29 CORRECTION (independent-review finding F-1, owner decision LOCKED): added
the exhaustive 1080-combination truth table (run_truth_table) plus the named
regression F1_AUTHORITATIVE_EVENT_MUST_NOT_BE_COUNTABLE, proving countable_allowed can
never be True while authoritative_duplicate is True -- the defect independent review
found in the pre-correction gate (event_type='duplicate_card' with duplicate=0/None
returned countable=True alongside is_duplicate=True).

    python tools\\w3_duplicate_status_gate_selftest.py
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def gate(**row):
    requested = row.pop("__requested_status", None)
    return storage.w3_duplicate_status_eligibility(row, requested_status=requested)


def run_all_checks() -> None:
    # ---- item 1: duplicate=0, lead_countable=1 -> ordinary allowed, countable allowed
    r = gate(duplicate=0, lead_countable=1, duplicate_checked=1)
    check("1: duplicate=0,lead_countable=1 -> ordinary allowed", r["allows_ordinary_status"] is True, str(r))
    check("1: duplicate=0,lead_countable=1 -> countable allowed", r["countable"] is True, str(r))
    check("1: not is_duplicate", r["is_duplicate"] is False)

    # ---- item 2: duplicate=1, lead_countable=1 -> ordinary blocked, countable False
    r = gate(duplicate=1, lead_countable=1, duplicate_checked=1)
    check("2: duplicate=1,lead_countable=1 -> ordinary blocked", r["allows_ordinary_status"] is False, str(r))
    check("2: duplicate=1,lead_countable=1 -> countable False", r["countable"] is False, str(r))

    # ---- item 3: duplicate=1, lead_countable=0 -> ordinary blocked, countable False
    r = gate(duplicate=1, lead_countable=0, duplicate_checked=1)
    check("3: duplicate=1,lead_countable=0 -> ordinary blocked", r["allows_ordinary_status"] is False, str(r))
    check("3: duplicate=1,lead_countable=0 -> countable False", r["countable"] is False, str(r))

    # ---- item 4: duplicate_card event without a daily_leads row -> authoritative duplicate
    r = gate(event_type="duplicate_card")
    check("4: duplicate_card event, no daily_leads row -> authoritative duplicate",
          r["is_duplicate"] is True and r["authoritative_signal"] == "DUP_AUTHORITATIVE_EVENT", str(r))

    # ---- item 5: duplicate_checked=0 + each provisional signal independently -> write blocked only
    r = gate(duplicate=0, duplicate_checked=0, lead_countable=0)
    check("5a (S-3 alone): write-blocked only, not classified as duplicate",
          r["pending_block"] is True and r["is_duplicate"] is False, str(r))
    r = gate(duplicate=0, duplicate_checked=0, contact_kind="returning")
    check("5b (S-4 alone): write-blocked only, not classified as duplicate",
          r["pending_block"] is True and r["is_duplicate"] is False, str(r))
    r = gate(duplicate=0, duplicate_checked=0, dedupe_reason="skipped: other_manager already has this contact")
    check("5c (S-5 alone): write-blocked only, not classified as duplicate",
          r["pending_block"] is True and r["is_duplicate"] is False, str(r))

    # ---- item 6: duplicate_checked=0 + ALL provisional signals together -> still provisional only
    r = gate(duplicate=0, duplicate_checked=0, lead_countable=0, contact_kind="old_baseline",
             dedupe_reason="other_manager")
    check("6: all provisional signals together -> still is_duplicate=False",
          r["is_duplicate"] is False, str(r))
    check("6: all provisional signals together -> pending_block True",
          r["pending_block"] is True, str(r))
    check("6: all three pending signals present",
          set(r["pending_signals"]) == {"DUP_PENDING_NOT_COUNTABLE", "DUP_PENDING_CONTACT_KIND",
                                         "DUP_PENDING_DEDUPE_REASON"}, str(r["pending_signals"]))

    # ---- item 7: duplicate_checked=1 + each provisional signal independently -> all ignored
    for kwargs, label in (
        ({"lead_countable": 0}, "7a (S-3)"),
        ({"contact_kind": "returning"}, "7b (S-4)"),
        ({"dedupe_reason": "other_manager"}, "7c (S-5)"),
    ):
        r = gate(duplicate=0, duplicate_checked=1, **kwargs)
        check(f"{label}: duplicate_checked=1 -> tier2 fully ignored",
              r["pending_block"] is False and r["is_duplicate"] is False and r["allows_ordinary_status"] is True,
              str(r))

    # ---- item 8: duplicate_checked=1 + ALL provisional signals -> ordinary allowed unless authoritative
    r = gate(duplicate=0, duplicate_checked=1, lead_countable=0, contact_kind="old_baseline",
             dedupe_reason="other_manager")
    check("8: duplicate_checked=1, all provisional signals, no authoritative -> ordinary allowed",
          r["allows_ordinary_status"] is True, str(r))
    r = gate(duplicate=1, duplicate_checked=1, lead_countable=0, contact_kind="old_baseline",
             dedupe_reason="other_manager")
    check("8b: duplicate_checked=1, provisional signals PLUS authoritative duplicate=1 -> still blocked",
          r["allows_ordinary_status"] is False and r["is_duplicate"] is True, str(r))

    # ---- item 9: authoritative duplicate with stale ordinary status
    r = gate(duplicate=1, status="liquid", manual_status_override=1, __requested_status=None)
    check("9: stale violation true", r["stale_ordinary_status"] is True, str(r))
    check("9: ordinary render/callback both false (via allows_ordinary_status)",
          r["allows_ordinary_status"] is False, str(r))
    check("9: forced_state == 'duplicate' (dedicated duplicate state)", r["forced_state"] == "duplicate", str(r))
    check("9: violation_code == DUP_STALE_ORDINARY_STATUS", r["violation_code"] == "DUP_STALE_ORDINARY_STATUS",
          str(r))

    # ---- item 10: authoritative duplicate with prior-recipient history
    # W3.1's gate does not itself carry "prior recipient" history (that is ManagerBot's
    # _format_duplicate_card payload, out of storage.py's scope) -- the gate's own
    # contribution is that duplicate_history_required-equivalent information (I-48: the
    # duplicate flag and its history) is never touched by this pure function. Verified
    # as "no mutation" below (item 13) plus explicit non-tampering here.
    row = {"duplicate": 1, "prev_manager_key": "otherMgr", "prev_first_contact": "2026-01-01"}
    r = storage.w3_duplicate_status_eligibility(row)
    check("10: gate does not touch/clear prior-recipient fields (I-48)",
          row["prev_manager_key"] == "otherMgr" and row["prev_first_contact"] == "2026-01-01", str(row))

    # ---- item 11: provisional signal never sets authoritative_duplicate
    r = gate(duplicate=0, duplicate_checked=0, lead_countable=0, contact_kind="returning",
             dedupe_reason="other_manager")
    check("11: provisional signals never set is_duplicate/authoritative_signal",
          r["is_duplicate"] is False and r["authoritative_signal"] == "", str(r))

    # ---- item 12: provisional signal never changes countable_allowed (i.e. `countable`)
    r_a = gate(duplicate=0, duplicate_checked=0, lead_countable=1)
    r_b = gate(duplicate=0, duplicate_checked=0, lead_countable=1, contact_kind="returning",
               dedupe_reason="other_manager")
    check("12: countable unaffected by tier-2 signals when lead_countable=1 in both",
          r_a["countable"] == r_b["countable"] == True, f"{r_a['countable']} vs {r_b['countable']}")

    # ---- item 13: no input combination writes or returns a mutation request for duplicate=1
    # Pure function -- static proof: source contains no SQL / no 'con.execute' / no
    # storage.* write call inside the gate's definition span.
    src = Path(BASE_DIR, "storage.py").read_text(encoding="utf-8")
    start = src.index("def w3_duplicate_status_eligibility")
    end = src.index("\ndef w3_countable")
    gate_src = src[start:end]
    for forbidden in ("con.execute", "INSERT INTO", "UPDATE ", "DELETE FROM", "_bsl_connect",
                       "cur.execute"):
        check(f"13: gate body contains no {forbidden!r} (pure function, no I/O)",
              forbidden not in gate_src, "found!" if forbidden in gate_src else "")

    # ---- item 14: deterministic repeated calls -> byte-equivalent canonical JSON
    row14 = {"duplicate": 1, "lead_countable": 1, "contact_kind": "new", "duplicate_checked": 1}
    j1 = storage._w3_json_dumps(storage.w3_duplicate_status_eligibility(dict(row14)))
    j2 = storage._w3_json_dumps(storage.w3_duplicate_status_eligibility(dict(row14)))
    check("14: repeated calls produce byte-identical canonical JSON", j1 == j2, f"{j1!r} vs {j2!r}")
    # J14 (Plan Freeze numbering): the C-11 case itself
    r14 = gate(duplicate=1, lead_countable=1, contact_kind="new", duplicate_checked=1)
    check("J14: duplicate=1 AND lead_countable=1 -> is_duplicate=True, countable=False, DUP_AUTHORITATIVE_FLAG",
          r14["is_duplicate"] is True and r14["countable"] is False
          and r14["authoritative_signal"] == "DUP_AUTHORITATIVE_FLAG", str(r14))

    # ---- item 15: C-11 synthetic delta proof (N_dup_only / N_106 / residual == 0)
    synthetic_rows = (
        [{"duplicate": 1, "lead_countable": 1, "contact_kind": "new"} for _ in range(4)]   # N_106 population
        + [{"duplicate": 1, "lead_countable": 0} for _ in range(6)]                        # N_dup_only population
        + [{"duplicate": 0, "lead_countable": 1} for _ in range(20)]                       # ordinary, unaffected
    )
    n_106 = sum(1 for row in synthetic_rows if row.get("duplicate") == 1 and row.get("lead_countable") == 1)
    n_dup_only = sum(1 for row in synthetic_rows if row.get("duplicate") == 1 and row.get("lead_countable") != 1)
    # "before" baseline = a fully UNFILTERED total (e.g. C5 "LIGHT Total written" --
    # stats_engine.py:704, `total = len(leads_all)`, no duplicate predicate at all),
    # which is exactly the population the delta-completeness formula in 16.10.1
    # measures against: delta_total = N_dup_only + N_106 requires "before" to include
    # BOTH populations, since neither was excluded prior to the W3.5-B fix.
    total_before = len(synthetic_rows)
    total_after = sum(1 for row in synthetic_rows if storage.w3_duplicate_status_eligibility(row)["countable"])
    delta_total = total_before - total_after
    residual = delta_total - n_dup_only - n_106
    check("15: N_106 == 4 (duplicate=1 AND lead_countable=1)", n_106 == 4, str(n_106))
    check("15: N_dup_only == 6 (duplicate=1 AND lead_countable!=1)", n_dup_only == 6, str(n_dup_only))
    check("15: residual == delta_total - N_dup_only - N_106 == 0", residual == 0,
          f"delta_total={delta_total} n_dup_only={n_dup_only} n_106={n_106} residual={residual}")
    # J18 alias
    check("J18: delta completeness residual == 0", residual == 0, str(residual))

    # ---- item 16: C-12 -- stale status preserved in input, output suppresses it, no
    # delete/mutation instruction returned
    row16 = {"duplicate": 1, "status": "liquid", "quality_status": "good"}
    row16_copy = dict(row16)
    r16 = storage.w3_duplicate_status_eligibility(row16)
    check("16: input row is not mutated by the gate call", row16 == row16_copy, str(row16))
    check("16: stale fields are REPORTED (stale_status_fields non-empty), not erased",
          set(r16["stale_status_fields"]) >= {"status", "quality_status"}, str(r16["stale_status_fields"]))
    check("16: no key in the gate's return value instructs a delete/mutation "
          "(only booleans/strings/lists of codes)",
          all(not (isinstance(v, dict) and "delete" in str(v).lower()) for v in r16.values()), str(r16))

    # ---- J15/J16/J17: individual tier-2 signal tests with exact expected pending_signals
    r = gate(duplicate=0, duplicate_checked=0, lead_countable=0)
    check("J15: S-3 alone -> pending_signals == ['DUP_PENDING_NOT_COUNTABLE'], forced_state=''",
          r["pending_signals"] == ["DUP_PENDING_NOT_COUNTABLE"] and r["forced_state"] == "", str(r))
    r = gate(duplicate=0, duplicate_checked=0, contact_kind="returning")
    check("J16: S-4 alone -> pending_signals == ['DUP_PENDING_CONTACT_KIND']",
          r["pending_signals"] == ["DUP_PENDING_CONTACT_KIND"], str(r))
    r = gate(duplicate=0, duplicate_checked=0, dedupe_reason="handoff: other_manager claimed first")
    check("J17: S-5 alone -> pending_signals == ['DUP_PENDING_DEDUPE_REASON']",
          r["pending_signals"] == ["DUP_PENDING_DEDUPE_REASON"], str(r))

    # ---- J19: P5 rollback rule
    r = gate(duplicate=0, duplicate_checked=1, lead_countable=0, contact_kind="returning",
             dedupe_reason="other_manager")
    check("J19: P5 -- duplicate_checked=1 with all tier-2 signals active -> fully ignored, allowed",
          r["is_duplicate"] is False and r["pending_block"] is False and r["allows_ordinary_status"] is True,
          str(r))

    # ---- J20: P4 -- no tier-2 signal writes duplicate=1 (static + dynamic)
    check("J20 (static): no 'duplicate' assignment/UPDATE literal in the gate body",
          "duplicate=1" not in gate_src and "duplicate = 1" not in gate_src, "")
    row20 = {"duplicate": 0, "duplicate_checked": 0, "lead_countable": 0}
    storage.w3_duplicate_status_eligibility(row20, requested_status="liquid")
    check("J20 (dynamic): duplicate value on the caller's row is unchanged after a block",
          row20["duplicate"] == 0, str(row20))

    # ---- J21: P2 -- tier-2 signals do not affect statistics (row counted normally)
    r = gate(duplicate=0, duplicate_checked=0, contact_kind="returning", lead_countable=1)
    check("J21: tier-2-only row is counted normally (countable=True)", r["countable"] is True, str(r))

    # ---- J22: S-2 exact contract
    r = gate(event_type="duplicate_card")
    check("J22: event_type=duplicate_card, no other fields -> is_duplicate + DUP_AUTHORITATIVE_EVENT",
          r["is_duplicate"] is True and r["authoritative_signal"] == "DUP_AUTHORITATIVE_EVENT", str(r))

    # ---- write-rejection reason code shape (A3-style, no raw ids ever appear)
    r = gate(duplicate=1, tg_user_id=123456789, __requested_status="liquid")
    check("write rejection carries a pseudonymized actor_ref, never the raw tg_user_id",
          r["actor_ref"] != "" and "123456789" not in r["actor_ref"], str(r["actor_ref"]))
    check("write rejection violation_code == DUP_ORDINARY_STATUS_WRITE_REJECTED",
          r["violation_code"] == "DUP_ORDINARY_STATUS_WRITE_REJECTED", str(r))

    # ---- w3_countable thin wrapper matches the field
    for row in (
        {"duplicate": 1, "lead_countable": 1}, {"duplicate": 0, "lead_countable": 1},
        {"duplicate": 0, "lead_countable": 0}, {"event_type": "duplicate_card"},
    ):
        full = storage.w3_duplicate_status_eligibility(row)
        thin = storage.w3_countable(row)
        check(f"w3_countable matches full gate's 'countable' field for {row}", thin == full["countable"])

    # ---- purity: no clock read, no randomness sensitivity -- same input across two
    # calls separated by real work in between still matches
    row_p = {"duplicate": 1, "lead_countable": 1}
    a = storage.w3_duplicate_status_eligibility(dict(row_p))
    for _ in range(1000):
        pass
    b = storage.w3_duplicate_status_eligibility(dict(row_p))
    check("purity: identical input -> identical output across time", a == b, f"{a} vs {b}")

    # ---- F1_AUTHORITATIVE_EVENT_MUST_NOT_BE_COUNTABLE: the exact independent-review
    # F-1 regression. Pre-correction, countable was derived from the raw `duplicate`
    # field alone; an event_type='duplicate_card' row with duplicate 0/None/absent
    # returned is_duplicate=True (correctly) but countable=True (contradiction).
    for label, row in (
        ("F1a: duplicate=0, event=duplicate_card", {"duplicate": 0, "event_type": "duplicate_card"}),
        ("F1b: duplicate missing, event=duplicate_card", {"event_type": "duplicate_card"}),
        ("F1c: duplicate=None, event=duplicate_card, lead_countable=1",
         {"duplicate": None, "event_type": "duplicate_card", "lead_countable": 1}),
    ):
        r = gate(**row)
        check(f"F1_AUTHORITATIVE_EVENT_MUST_NOT_BE_COUNTABLE ({label})",
              r["is_duplicate"] is True and r["authoritative_duplicate"] is True
              and r["countable"] is False and r["countable_allowed"] is False, str(r))

    # ---- owner-locked correction required-behavior cases A-E (verbatim from the
    # correction task's section 1)
    rA = gate(duplicate=1)
    check("A: duplicate=1, ordinary event -> authoritative_duplicate=True, countable_allowed=False",
          rA["authoritative_duplicate"] is True and rA["countable_allowed"] is False, str(rA))
    rB = gate(duplicate=0, event_type="duplicate_card")
    check("B: duplicate=0, event=duplicate_card -> authoritative_duplicate=True, countable_allowed=False",
          rB["authoritative_duplicate"] is True and rB["countable_allowed"] is False, str(rB))
    rC = gate(event_type="duplicate_card")
    check("C: duplicate missing/None, event=duplicate_card -> authoritative_duplicate=True, countable_allowed=False",
          rC["authoritative_duplicate"] is True and rC["countable_allowed"] is False, str(rC))
    rD1 = gate(duplicate=0, duplicate_checked=0, lead_countable=1)
    rD2 = gate(duplicate=0, duplicate_checked=0, lead_countable=1, contact_kind="returning",
               dedupe_reason="other_manager")
    check("D: provisional-only signals -> authoritative_duplicate=False",
          rD1["authoritative_duplicate"] is False and rD2["authoritative_duplicate"] is False, str((rD1, rD2)))
    check("D: provisional signals do not themselves force countable_allowed False "
          "(only the real lead_countable field does)",
          rD1["countable_allowed"] == rD2["countable_allowed"] == True, str((rD1["countable_allowed"], rD2["countable_allowed"])))
    rE = gate(duplicate=0, duplicate_checked=1, lead_countable=0, contact_kind="returning",
              dedupe_reason="other_manager")
    check("E: duplicate_checked=1 -> provisional signals ignored completely (write allowed too)",
          rE["pending_block"] is False and rE["allows_ordinary_status"] is True
          and rE["ordinary_status_write_allowed"] is True, str(rE))


def run_truth_table() -> None:
    """Exhaustive 1080-combination truth table (independent-review method,
    2026-07-29 correction), asserting the corrected countability contract holds on
    every combination -- not just the hand-picked cases above."""
    DUP = (0, 1)
    CHECKED = (0, 1)
    EVT = ("", "lead_card", "duplicate_card")
    LC = (None, 0, 1)
    CK = ("", "new", "duplicate", "returning", "old_baseline")
    DR = ("", "same_manager", "other_manager:x")
    REQ_ST = (None, "in_work")

    n = 0
    auth_countable_violations = []
    provisional_classified_dup = []
    provisional_countable_changed = []
    checked_ignores_provisional = []
    for dup, chkd, evt, lc, ck, dr, rq in itertools.product(DUP, CHECKED, EVT, LC, CK, DR, REQ_ST):
        row = {"duplicate": dup, "duplicate_checked": chkd, "event_type": evt,
               "lead_countable": lc, "contact_kind": ck, "dedupe_reason": dr}
        g = storage.w3_duplicate_status_eligibility(row, requested_status=rq)
        n += 1
        auth = (dup == 1) or (evt == "duplicate_card")
        if g["authoritative_duplicate"] != auth:
            provisional_classified_dup.append((row, g))
        if auth and (g["countable"] is not False or g["countable_allowed"] is not False):
            auth_countable_violations.append((row, g["countable"], g["countable_allowed"]))
        if not auth:
            expected_countable = (lc is None) or (int(lc or 0) != 0)
            if g["countable_allowed"] != expected_countable:
                provisional_countable_changed.append((row, g["countable_allowed"], expected_countable))
        if chkd == 1 and g["pending_block"] is not False:
            checked_ignores_provisional.append((row, g))

    check(f"truth table: exhaustive 1080 combinations enumerated (got {n})", n == 1080, str(n))
    check("truth table: NO combination has authoritative_duplicate=True with countable/countable_allowed=True",
          not auth_countable_violations,
          f"{len(auth_countable_violations)} violation(s), sample={auth_countable_violations[:3]}")
    check("truth table: authoritative_duplicate classification matches (dup==1) OR (event=='duplicate_card') exactly",
          not provisional_classified_dup,
          f"{len(provisional_classified_dup)} violation(s), sample={provisional_classified_dup[:2]}")
    check("truth table: non-authoritative countable_allowed governed only by lead_countable, "
          "never by contact_kind/dedupe_reason (P2)",
          not provisional_countable_changed,
          f"{len(provisional_countable_changed)} violation(s), sample={provisional_countable_changed[:2]}")
    check("truth table: duplicate_checked=1 fully disables pending_block on every combination",
          not checked_ignores_provisional,
          f"{len(checked_ignores_provisional)} violation(s)")


def main() -> int:
    run_all_checks()
    run_truth_table()
    print()
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} failing check(s))")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS (all checks green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
