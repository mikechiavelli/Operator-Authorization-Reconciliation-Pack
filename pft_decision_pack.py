"""
pft_decision_pack.py
====================

Operator-grade decision pack for Post Fiat task / wallet / event reconciliation.

Self-contained, no private dependencies. Embeds fixture records for task state,
wallet authorization state, and event history, then produces machine-readable
JSON decisions with explicit reason codes, authoritative source, reconciliation
status, and rollback action so a reviewer can audit:

  * unauthorized contributor attempts
  * cooldown bypass
  * duplicate reward paths
  * stale context conflicts
  * replayed events
  * rescore triggers

Run:
    python pft_decision_pack.py
    python pft_decision_pack.py --pretty

Exit code is 0 on a successful evaluation (regardless of decision mix).

------------------------------------------------------------------------------
DECISION SCHEMA
------------------------------------------------------------------------------

decision           : one of {"allow", "hold", "block", "rescore"}
reason_codes       : sorted list drawn from REASON_CODES below
authoritative_source : which fixture domain won precedence for this decision,
                       one of {"wallet_auth", "task_state", "event_history",
                               "context_state", "composite"}
reconciliation_status : one of {"clean", "reconciled", "conflict_unresolved"}
rollback_action    : machine-readable hint for the operator console, one of
                       {"none", "revert_reward", "freeze_task",
                        "requeue_for_rescore", "quarantine_event",
                        "refresh_context"}
operator_note      : short human-readable explanation
case_id            : fixture identifier (echoed for audit)

SOURCE PRECEDENCE (highest first)
---------------------------------
1. wallet_auth        - if the wallet is not on the authorized contributor
                        list for the task's sector, nothing else matters; we
                        block. Ownership claim mismatch (wallet authorized
                        but not the current task owner) lives here too and
                        produces a hold for reconciliation.
2. event_history      - if the exact (task_id, wallet, nonce) tuple has been
                        seen before with a terminal outcome, we treat it as a
                        replay; this dominates task_state because the ledger
                        is the canonical record of what already happened.
                        If the prior outcome was a reward, both replayed_event
                        and duplicate_reward_path are accumulated and the
                        authoritative_source becomes 'composite'.
3. task_state         - drives cooldown / duplicate-reward / rescore logic
                        when wallet and event history are clean.
4. context_state      - staleness check; can demote an otherwise-allow to
                        hold when the task's context snapshot is older than
                        the wallet's last context refresh.

REASON CODES
------------
unauthorized_contributor   wallet not in sector allowlist
cooldown_bypass            submission inside the cooldown window
duplicate_reward_path      reward already paid for this task+wallet
stale_context_conflict     context_state older than wallet refresh
stale_ownership_conflict   submitting wallet is not the current task owner
replayed_event             (task_id, wallet, nonce) already terminal
rescore_trigger            task scorecard version older than active rubric
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Reason code constants (single source of truth)
# ---------------------------------------------------------------------------

R_UNAUTHORIZED       = "unauthorized_contributor"
R_COOLDOWN_BYPASS    = "cooldown_bypass"
R_DUPLICATE_REWARD   = "duplicate_reward_path"
R_STALE_CONTEXT      = "stale_context_conflict"
R_STALE_OWNERSHIP    = "stale_ownership_conflict"
R_REPLAYED_EVENT     = "replayed_event"
R_RESCORE_TRIGGER    = "rescore_trigger"

REASON_CODES = frozenset({
    R_UNAUTHORIZED,
    R_COOLDOWN_BYPASS,
    R_DUPLICATE_REWARD,
    R_STALE_CONTEXT,
    R_STALE_OWNERSHIP,
    R_REPLAYED_EVENT,
    R_RESCORE_TRIGGER,
})

# Decisions
D_ALLOW   = "allow"
D_HOLD    = "hold"
D_BLOCK   = "block"
D_RESCORE = "rescore"

# Authoritative sources
S_WALLET   = "wallet_auth"
S_TASK     = "task_state"
S_EVENT    = "event_history"
S_CONTEXT  = "context_state"
S_COMPOSITE = "composite"

# Rollback actions
RB_NONE        = "none"
RB_REVERT      = "revert_reward"
RB_FREEZE      = "freeze_task"
RB_REQUEUE     = "requeue_for_rescore"
RB_QUARANTINE  = "quarantine_event"
RB_REFRESH_CTX = "refresh_context"


# ---------------------------------------------------------------------------
# Embedded fixtures
# ---------------------------------------------------------------------------
#
# Three fixture domains are embedded:
#
#   TASKS         - the current task state (cooldown, paid flag, rubric ver)
#   WALLETS       - per-wallet authorization & last context refresh time
#   EVENT_HISTORY - terminal events the ledger has already seen (for replay
#                   and duplicate-reward checks)
#
# CASES then names the (task_id, wallet, nonce, submitted_at, ...) tuple to
# evaluate. Times are integer epoch seconds for determinism.

ACTIVE_RUBRIC_VERSION = 5         # current scorecard rubric version
COOLDOWN_SECONDS      = 3600      # 1h cooldown per (task, wallet)
CONTEXT_TTL_SECONDS   = 86_400    # 24h - context_state must be newer than
                                  # wallet.last_context_refresh - TTL

TASKS: Dict[str, Dict[str, Any]] = {
    "TASK-AERO-001": {
        "sector": "defi",
        "rubric_version": 5,
        "last_submission_at": 1_700_000_000,   # well in the past
        "reward_paid": False,
        "context_snapshot_at": 1_730_000_000,  # fresh-ish
        "claimed_by": None,
    },
    "TASK-AERO-002": {
        "sector": "defi",
        "rubric_version": 5,
        "last_submission_at": 1_730_003_000,   # very recent
        "reward_paid": False,
        "context_snapshot_at": 1_730_000_000,
        "claimed_by": None,
    },
    "TASK-VVV-010": {
        "sector": "ai_agent",
        "rubric_version": 5,
        "last_submission_at": 1_700_000_000,
        "reward_paid": True,                   # already paid
        "context_snapshot_at": 1_730_000_000,
        "claimed_by": None,
    },
    "TASK-FIL-021": {
        "sector": "depin",
        "rubric_version": 3,                   # OUTDATED rubric
        "last_submission_at": 1_700_000_000,
        "reward_paid": False,
        "context_snapshot_at": 1_730_000_000,
        "claimed_by": None,
    },
    "TASK-PENGU-033": {
        "sector": "memecoin",
        "rubric_version": 5,
        "last_submission_at": 1_700_000_000,
        "reward_paid": False,
        # context_snapshot is OLDER than wallet refresh => stale
        "context_snapshot_at": 1_690_000_000,
        "claimed_by": None,
    },
    "TASK-RENDER-044": {
        "sector": "ai_compute",
        "rubric_version": 5,
        "last_submission_at": 1_700_000_000,
        "reward_paid": False,
        "context_snapshot_at": 1_730_000_000,
        "claimed_by": None,
    },
    "TASK-AR-055": {
        "sector": "storage",
        "rubric_version": 5,
        "last_submission_at": 1_700_000_000,
        "reward_paid": False,
        "context_snapshot_at": 1_730_000_000,
        "claimed_by": None,
    },
    "TASK-TAO-066": {
        "sector": "ai_agent",
        "rubric_version": 5,
        "last_submission_at": 1_700_000_000,
        "reward_paid": False,
        "context_snapshot_at": 1_730_000_000,
        # Claimed by ALPHA - drives stale_ownership case when BETA submits
        "claimed_by": "rWALLET_ALPHA",
    },
    "TASK-OLAS-077": {
        # Both cooldown AND rescore fire - drives composite case
        "sector": "ai_agent",
        "rubric_version": 4,                   # outdated
        "last_submission_at": 1_730_003_500,   # very recent => cooldown
        "reward_paid": False,
        "context_snapshot_at": 1_730_000_000,
        "claimed_by": None,
    },
}

# Wallets and which sectors they are authorized to contribute to.
WALLETS: Dict[str, Dict[str, Any]] = {
    "rWALLET_ALPHA": {
        "authorized_sectors": ["defi", "ai_agent", "ai_compute",
                               "storage", "depin", "memecoin"],
        "last_context_refresh_at": 1_729_950_000,
    },
    "rWALLET_BETA": {
        "authorized_sectors": ["defi", "ai_agent", "storage", "depin"],
        "last_context_refresh_at": 1_729_950_000,
    },
    "rWALLET_GAMMA": {
        # Not authorized for storage - used for unauthorized case
        "authorized_sectors": ["defi"],
        "last_context_refresh_at": 1_729_950_000,
    },
    "rWALLET_DELTA": {
        # Recent context refresh - used to expose stale task context
        "authorized_sectors": ["memecoin", "defi"],
        "last_context_refresh_at": 1_729_999_000,
    },
}

# Terminal events already on ledger. Keyed by (task_id, wallet, nonce).
EVENT_HISTORY: List[Dict[str, Any]] = [
    {
        "task_id": "TASK-VVV-010",
        "wallet":  "rWALLET_ALPHA",
        "nonce":   "n-0001",
        "outcome": "rewarded",
        "at":      1_729_000_000,
    },
    {
        # Used by replay case
        "task_id": "TASK-RENDER-044",
        "wallet":  "rWALLET_ALPHA",
        "nonce":   "n-0099",
        "outcome": "rewarded",
        "at":      1_729_500_000,
    },
    {
        # Used by duplicate-event case (same nonce will arrive again below)
        "task_id": "TASK-AR-055",
        "wallet":  "rWALLET_BETA",
        "nonce":   "n-0042",
        "outcome": "rewarded",
        "at":      1_729_700_000,
    },
]

# Cases to evaluate. Each is a candidate event being submitted *now*.
NOW = 1_730_005_000  # frozen "now" for deterministic decisions

CASES: List[Dict[str, Any]] = [
    # 1. Clean allow
    {
        "case_id":      "CASE-001-clean-allow",
        "task_id":      "TASK-AERO-001",
        "wallet":       "rWALLET_ALPHA",
        "nonce":        "n-1001",
        "submitted_at": NOW,
    },
    # 2. Unauthorized contributor (gamma not in storage allowlist)
    {
        "case_id":      "CASE-002-unauthorized",
        "task_id":      "TASK-AR-055",
        "wallet":       "rWALLET_GAMMA",
        "nonce":        "n-1002",
        "submitted_at": NOW,
    },
    # 3. Cooldown bypass (TASK-AERO-002 last submission was ~2000s ago)
    {
        "case_id":      "CASE-003-cooldown-bypass",
        "task_id":      "TASK-AERO-002",
        "wallet":       "rWALLET_ALPHA",
        "nonce":        "n-1003",
        "submitted_at": NOW,
    },
    # 4. Duplicate reward path (VVV-010 already paid)
    {
        "case_id":      "CASE-004-duplicate-reward",
        "task_id":      "TASK-VVV-010",
        "wallet":       "rWALLET_ALPHA",
        "nonce":        "n-1004",
        "submitted_at": NOW,
    },
    # 5. Stale context conflict (PENGU snapshot older than wallet refresh)
    {
        "case_id":      "CASE-005-stale-context",
        "task_id":      "TASK-PENGU-033",
        "wallet":       "rWALLET_DELTA",
        "nonce":        "n-1005",
        "submitted_at": NOW,
    },
    # 6. Replayed event (exact tuple already terminal on ledger)
    {
        "case_id":      "CASE-006-replayed-event",
        "task_id":      "TASK-RENDER-044",
        "wallet":       "rWALLET_ALPHA",
        "nonce":        "n-0099",   # collides with EVENT_HISTORY entry
        "submitted_at": NOW,
    },
    # 7. Rescore trigger (FIL-021 rubric_version=3, active=5)
    {
        "case_id":      "CASE-007-rescore-trigger",
        "task_id":      "TASK-FIL-021",
        "wallet":       "rWALLET_ALPHA",
        "nonce":        "n-1007",
        "submitted_at": NOW,
    },
    # 8. Duplicate-event reconciliation (BETA already has nonce n-0042 paid
    #    on AR-055; this resubmission is identical => quarantine)
    {
        "case_id":      "CASE-008-duplicate-event-reconcile",
        "task_id":      "TASK-AR-055",
        "wallet":       "rWALLET_BETA",
        "nonce":        "n-0042",
        "submitted_at": NOW,
    },
    # 9. Composite: cooldown AND rescore both fire - exercises precedence
    #    and proves multiple reason_codes can co-exist deterministically.
    {
        "case_id":      "CASE-009-composite",
        "task_id":      "TASK-OLAS-077",   # cooldown + outdated rubric
        "wallet":       "rWALLET_BETA",
        "nonce":        "n-1009",
        "submitted_at": NOW,
    },
    # 10. Stale ownership: TASK-TAO-066 is claimed by ALPHA, BETA is
    #     authorized for ai_agent but is not the current owner.
    {
        "case_id":      "CASE-010-stale-ownership",
        "task_id":      "TASK-TAO-066",
        "wallet":       "rWALLET_BETA",
        "nonce":        "n-1010",
        "submitted_at": NOW,
    },
    # 11. Unknown task: exercises the defensive branch when the case
    #     references a task the system has never heard of.
    {
        "case_id":      "CASE-011-unknown-task",
        "task_id":      "TASK-DOES-NOT-EXIST",
        "wallet":       "rWALLET_ALPHA",
        "nonce":        "n-1011",
        "submitted_at": NOW,
    },
]


# ---------------------------------------------------------------------------
# Decision engine
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    case_id:               str
    decision:              str = D_ALLOW
    reason_codes:          List[str] = field(default_factory=list)
    authoritative_source:  str = S_TASK
    reconciliation_status: str = "clean"
    rollback_action:       str = RB_NONE
    operator_note:         str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id":               self.case_id,
            "decision":              self.decision,
            "reason_codes":          sorted(set(self.reason_codes)),
            "authoritative_source":  self.authoritative_source,
            "reconciliation_status": self.reconciliation_status,
            "rollback_action":       self.rollback_action,
            "operator_note":         self.operator_note,
        }


def _find_event(task_id: str, wallet: str, nonce: str
                ) -> Optional[Dict[str, Any]]:
    """Return the terminal event for this exact tuple, if any."""
    for ev in EVENT_HISTORY:
        if (ev["task_id"] == task_id
                and ev["wallet"] == wallet
                and ev["nonce"] == nonce):
            return ev
    return None


def _wallet_paid_task(task_id: str, wallet: str) -> bool:
    """Has this wallet already been rewarded for this task under any nonce?"""
    for ev in EVENT_HISTORY:
        if (ev["task_id"] == task_id
                and ev["wallet"] == wallet
                and ev["outcome"] == "rewarded"):
            return True
    return False


def evaluate(case: Dict[str, Any]) -> Decision:
    """
    Evaluate one candidate event against TASKS / WALLETS / EVENT_HISTORY.

    Source precedence (highest first):
        1. wallet_auth     -> hard block on unauthorized_contributor
        1b. ownership      -> hold on stale_ownership_conflict
        2. event_history   -> hard block on replayed_event (composite if
                              prior outcome was a reward)
        3. task_state      -> cooldown / duplicate-reward / rescore
        4. context_state   -> staleness demotes allow -> hold
    """
    d = Decision(case_id=case["case_id"])

    task_id      = case["task_id"]
    wallet_id    = case["wallet"]
    nonce        = case["nonce"]
    submitted_at = case["submitted_at"]

    task   = TASKS.get(task_id)
    wallet = WALLETS.get(wallet_id)

    if task is None or wallet is None:
        d.decision              = D_BLOCK
        d.reason_codes.append(R_UNAUTHORIZED)
        d.authoritative_source  = S_WALLET
        d.reconciliation_status = "conflict_unresolved"
        d.rollback_action       = RB_QUARANTINE
        d.operator_note         = "unknown task or wallet; cannot reconcile"
        return d

    # -- 1. wallet_auth precedence -----------------------------------------
    if task["sector"] not in wallet["authorized_sectors"]:
        d.decision              = D_BLOCK
        d.reason_codes.append(R_UNAUTHORIZED)
        d.authoritative_source  = S_WALLET
        d.reconciliation_status = "reconciled"
        d.rollback_action       = RB_QUARANTINE
        d.operator_note = (
            f"wallet {wallet_id} not authorized for sector "
            f"'{task['sector']}'"
        )
        return d

    # -- 1b. stale ownership ----------------------------------------------
    # If the task is currently claimed by a specific wallet and a different
    # wallet is submitting, ownership state is stale relative to this
    # event. This is distinct from sector-level unauthorized: the wallet
    # has the right sector permission, just not the active claim.
    claimed_by = task.get("claimed_by")
    if claimed_by is not None and claimed_by != wallet_id:
        d.decision              = D_HOLD
        d.reason_codes.append(R_STALE_OWNERSHIP)
        d.authoritative_source  = S_TASK
        d.reconciliation_status = "conflict_unresolved"
        d.rollback_action       = RB_FREEZE
        d.operator_note = (
            f"task {task_id} is currently claimed by {claimed_by}; "
            f"submitting wallet {wallet_id} is not the owner - "
            "freeze and reconcile claim state before allowing"
        )
        return d

    # -- 2. event_history precedence ---------------------------------------
    prior = _find_event(task_id, wallet_id, nonce)
    if prior is not None:
        # A literal replay. If the prior outcome was a reward, this is also
        # a duplicate-reward attempt - accumulate both reason codes and
        # mark the source as composite so the operator sees the full
        # reconciliation story rather than just the dominant signal.
        codes = [R_REPLAYED_EVENT]
        is_dup_reward = (
            prior.get("outcome") == "rewarded"
            or task["reward_paid"]
            or _wallet_paid_task(task_id, wallet_id)
        )
        if is_dup_reward:
            codes.append(R_DUPLICATE_REWARD)

        d.decision              = D_BLOCK
        d.reason_codes          = codes
        d.authoritative_source  = S_COMPOSITE if is_dup_reward else S_EVENT
        d.reconciliation_status = "reconciled"
        # If a reward was paid, the rollback action must revert it; pure
        # replays without payout just need quarantine.
        d.rollback_action = RB_REVERT if is_dup_reward else RB_QUARANTINE
        if is_dup_reward:
            d.operator_note = (
                f"replayed (task,wallet,nonce) tuple already terminal as "
                f"'{prior['outcome']}' at {prior['at']}; reward already "
                "issued - revert before re-evaluating"
            )
        else:
            d.operator_note = (
                f"exact (task,wallet,nonce) tuple already terminal as "
                f"'{prior['outcome']}' at {prior['at']}"
            )
        return d

    # -- 3. task_state checks ----------------------------------------------
    reasons: List[str] = []

    # 3a. duplicate reward path - paid flag OR ledger shows reward to this
    # wallet. The ledger is more trustworthy than the flag, but either is a
    # block.
    if task["reward_paid"] or _wallet_paid_task(task_id, wallet_id):
        reasons.append(R_DUPLICATE_REWARD)

    # 3b. cooldown bypass
    elapsed = submitted_at - task["last_submission_at"]
    if elapsed < COOLDOWN_SECONDS:
        reasons.append(R_COOLDOWN_BYPASS)

    # 3c. rescore trigger
    rescore_needed = task["rubric_version"] < ACTIVE_RUBRIC_VERSION

    # -- 4. context_state staleness ----------------------------------------
    stale_context = (
        task["context_snapshot_at"]
        < wallet["last_context_refresh_at"] - CONTEXT_TTL_SECONDS
    )

    # -- decide ------------------------------------------------------------

    if R_DUPLICATE_REWARD in reasons:
        d.decision              = D_BLOCK
        d.reason_codes          = reasons
        d.authoritative_source  = S_EVENT if _wallet_paid_task(
            task_id, wallet_id) else S_TASK
        d.reconciliation_status = "reconciled"
        d.rollback_action       = RB_REVERT
        d.operator_note = (
            f"reward already issued for {task_id}/{wallet_id}; "
            "revert before re-evaluating"
        )
        return d

    if R_COOLDOWN_BYPASS in reasons and rescore_needed:
        # Composite: cooldown is the immediate-action signal, but the task
        # also needs a rescore. We hold (not block) so the operator can
        # requeue once the rubric is applied.
        d.decision              = D_HOLD
        d.reason_codes          = reasons + [R_RESCORE_TRIGGER]
        d.authoritative_source  = S_COMPOSITE
        d.reconciliation_status = "reconciled"
        d.rollback_action       = RB_REQUEUE
        d.operator_note = (
            f"cooldown active ({elapsed}s < {COOLDOWN_SECONDS}s) AND rubric "
            f"v{task['rubric_version']} < active v{ACTIVE_RUBRIC_VERSION}; "
            "hold for rescore"
        )
        return d

    if R_COOLDOWN_BYPASS in reasons:
        d.decision              = D_HOLD
        d.reason_codes          = reasons
        d.authoritative_source  = S_TASK
        d.reconciliation_status = "reconciled"
        d.rollback_action       = RB_FREEZE
        d.operator_note = (
            f"submitted {elapsed}s after last submission; cooldown is "
            f"{COOLDOWN_SECONDS}s"
        )
        return d

    if rescore_needed:
        d.decision              = D_RESCORE
        d.reason_codes          = [R_RESCORE_TRIGGER]
        d.authoritative_source  = S_TASK
        d.reconciliation_status = "reconciled"
        d.rollback_action       = RB_REQUEUE
        d.operator_note = (
            f"task rubric v{task['rubric_version']} < active "
            f"v{ACTIVE_RUBRIC_VERSION}; requeue for rescore"
        )
        return d

    if stale_context:
        d.decision              = D_HOLD
        d.reason_codes          = [R_STALE_CONTEXT]
        d.authoritative_source  = S_CONTEXT
        d.reconciliation_status = "conflict_unresolved"
        d.rollback_action       = RB_REFRESH_CTX
        d.operator_note = (
            f"task context snapshot at {task['context_snapshot_at']} is "
            f"older than wallet refresh "
            f"{wallet['last_context_refresh_at']} minus TTL "
            f"{CONTEXT_TTL_SECONDS}s; refresh context before allowing"
        )
        return d

    # Clean allow
    d.decision              = D_ALLOW
    d.reason_codes          = []
    d.authoritative_source  = S_TASK
    d.reconciliation_status = "clean"
    d.rollback_action       = RB_NONE
    d.operator_note         = "all checks passed"
    return d


# ---------------------------------------------------------------------------
# Self-verification
# ---------------------------------------------------------------------------
#
# EXPECTED is a hand-derived contract: for each case_id, what decision and
# reason_code set should the engine produce. test_decisions() runs the engine
# and asserts byte-for-byte agreement. This makes the module self-verifying
# and proves determinism (same input -> same output) without external tests.

EXPECTED: Dict[str, Tuple[str, frozenset, str]] = {
    # case_id -> (decision, reason_codes, authoritative_source)
    "CASE-001-clean-allow":              (D_ALLOW,   frozenset(),                                   S_TASK),
    "CASE-002-unauthorized":             (D_BLOCK,   frozenset({R_UNAUTHORIZED}),                   S_WALLET),
    "CASE-003-cooldown-bypass":          (D_HOLD,    frozenset({R_COOLDOWN_BYPASS}),                S_TASK),
    "CASE-004-duplicate-reward":         (D_BLOCK,   frozenset({R_DUPLICATE_REWARD}),               S_EVENT),
    "CASE-005-stale-context":            (D_HOLD,    frozenset({R_STALE_CONTEXT}),                  S_CONTEXT),
    "CASE-006-replayed-event":           (D_BLOCK,   frozenset({R_REPLAYED_EVENT,
                                                                R_DUPLICATE_REWARD}),               S_COMPOSITE),
    "CASE-007-rescore-trigger":          (D_RESCORE, frozenset({R_RESCORE_TRIGGER}),                S_TASK),
    "CASE-008-duplicate-event-reconcile":(D_BLOCK,   frozenset({R_REPLAYED_EVENT,
                                                                R_DUPLICATE_REWARD}),               S_COMPOSITE),
    "CASE-009-composite":                (D_HOLD,    frozenset({R_COOLDOWN_BYPASS,
                                                                R_RESCORE_TRIGGER}),                S_COMPOSITE),
    "CASE-010-stale-ownership":          (D_HOLD,    frozenset({R_STALE_OWNERSHIP}),                S_TASK),
    "CASE-011-unknown-task":             (D_BLOCK,   frozenset({R_UNAUTHORIZED}),                   S_WALLET),
}


def test_decisions() -> None:
    """
    Run the engine and assert each case matches its expected contract.
    Raises AssertionError on the first mismatch with a detailed message.
    Designed to be runnable as `python pft_decision_pack.py --test`.
    """
    result = run()
    decisions_by_id = {d["case_id"]: d for d in result["decisions"]}

    # Every expected case must be produced.
    missing = set(EXPECTED) - set(decisions_by_id)
    extra   = set(decisions_by_id) - set(EXPECTED)
    assert not missing, f"missing case decisions: {sorted(missing)}"
    assert not extra,   f"unexpected case decisions: {sorted(extra)}"

    for case_id, (exp_decision, exp_reasons, exp_source) in EXPECTED.items():
        got = decisions_by_id[case_id]
        assert got["decision"] == exp_decision, (
            f"{case_id}: decision={got['decision']!r}, "
            f"expected {exp_decision!r}"
        )
        assert frozenset(got["reason_codes"]) == exp_reasons, (
            f"{case_id}: reason_codes={got['reason_codes']!r}, "
            f"expected {sorted(exp_reasons)!r}"
        )
        assert got["authoritative_source"] == exp_source, (
            f"{case_id}: authoritative_source={got['authoritative_source']!r}, "
            f"expected {exp_source!r}"
        )

    # Every reason_codes value emitted must be drawn from the closed enum.
    for d in result["decisions"]:
        for rc in d["reason_codes"]:
            assert rc in REASON_CODES, (
                f"{d['case_id']}: reason_code {rc!r} not in REASON_CODES"
            )

    # Determinism: running twice produces byte-identical output.
    result2 = run()
    assert json.dumps(result, sort_keys=True) == json.dumps(
        result2, sort_keys=True
    ), "non-deterministic output across runs"

    print(f"OK - {len(EXPECTED)} cases verified, output is deterministic")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run() -> Dict[str, Any]:
    decisions = [evaluate(c).to_dict() for c in CASES]
    summary = Counter(d["decision"] for d in decisions)
    return {
        "active_rubric_version": ACTIVE_RUBRIC_VERSION,
        "cooldown_seconds":      COOLDOWN_SECONDS,
        "context_ttl_seconds":   CONTEXT_TTL_SECONDS,
        "evaluated_at":          NOW,
        "summary": {
            "total":   len(decisions),
            "allow":   summary.get(D_ALLOW, 0),
            "hold":    summary.get(D_HOLD, 0),
            "block":   summary.get(D_BLOCK, 0),
            "rescore": summary.get(D_RESCORE, 0),
        },
        "decisions": decisions,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Operator-grade decision pack for PFT reconciliation."
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="Pretty-print JSON output (indent=2).",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Run self-verifying assertions against expected outcomes.",
    )
    args = parser.parse_args(argv)

    if args.test:
        test_decisions()
        return 0

    result = run()
    if args.pretty:
        print(json.dumps(result, indent=2, sort_keys=False))
    else:
        print(json.dumps(result, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
