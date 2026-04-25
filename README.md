# PFT Decision Pack

A small operator-grade Python module that turns embedded task, wallet, and event-history fixtures into deterministic `allow` / `hold` / `block` / `rescore` decisions with explicit reason codes, authoritative source attribution, and rollback actions.

Self-contained — no private dependencies, no network calls, no config files. One `.py` file, embedded fixtures, machine-readable JSON output.

## What it does

For each candidate event (a `(task_id, wallet, nonce, submitted_at)` tuple), the engine checks the event against three notebooks of state:

- **TASKS** — current task state (sector, rubric version, last submission time, paid flag, context snapshot, claim ownership)
- **WALLETS** — per-wallet authorization (allowed sectors, last context refresh)
- **EVENT_HISTORY** — terminal events the ledger has already seen

It then emits a deterministic JSON decision per case, plus a summary count by outcome. Same input always produces byte-identical output.

## Running it

```bash
python pft_decision_pack.py            # one-line JSON for piping into jq, log aggregators, etc.
python pft_decision_pack.py --pretty   # indented JSON for humans
python pft_decision_pack.py --test     # self-verifying assertions; prints OK + case count
```

Exit code `0` on success in all modes. The `--test` mode runs the engine twice and asserts byte-identical output, proving determinism.

No installation required — Python 3.7+ standard library only.

## Decision schema

Every decision record has seven fields:

| Field                   | Purpose                                                      | Values                                                                                                        |
| ----------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| `case_id`               | Identifier for audit trail                                   | echoes the fixture id                                                                                         |
| `decision`              | High-level verdict                                           | `allow`, `hold`, `block`, `rescore`                                                                           |
| `reason_codes`          | Sorted list of why-codes that fired                          | drawn from the closed enum below                                                                              |
| `authoritative_source`  | Which notebook decided it                                    | `wallet_auth`, `event_history`, `task_state`, `context_state`, `composite`                                    |
| `reconciliation_status` | Whether the engine resolved cleanly                          | `clean`, `reconciled`, `conflict_unresolved`                                                                  |
| `rollback_action`       | Specific operator action                                     | `none`, `revert_reward`, `freeze_task`, `requeue_for_rescore`, `quarantine_event`, `refresh_context`          |
| `operator_note`         | Plain-English explanation                                    | free-form string                                                                                              |

### Reason codes

A closed enum. Every emitted reason code is drawn from this set; the `--test` mode enforces this.

| Code                        | Meaning                                                        |
| --------------------------- | -------------------------------------------------------------- |
| `unauthorized_contributor`  | Wallet not in sector allowlist                                 |
| `cooldown_bypass`           | Submission inside the cooldown window                          |
| `duplicate_reward_path`     | Reward already paid for this task+wallet                       |
| `stale_context_conflict`    | Task context snapshot older than wallet refresh minus TTL      |
| `stale_ownership_conflict`  | Submitting wallet is not the current task owner                |
| `replayed_event`            | Exact `(task_id, wallet, nonce)` tuple already terminal        |
| `rescore_trigger`           | Task scorecard version older than active rubric                |

## Source precedence

The engine walks checks in a fixed order. Higher-precedence sources short-circuit lower ones, except where the engine deliberately accumulates multiple reason codes into a `composite` source.

| Order | Source           | Triggers                                                                                       |
| ----- | ---------------- | ---------------------------------------------------------------------------------------------- |
| 1     | `wallet_auth`    | Hard `block` on `unauthorized_contributor`                                                     |
| 1b    | task ownership   | `hold` on `stale_ownership_conflict` — wallet is sector-authorized but not the current owner   |
| 2     | `event_history`  | `block` on `replayed_event`; if prior outcome was a reward, accumulates `duplicate_reward_path` and source becomes `composite` |
| 3     | `task_state`     | `cooldown_bypass`, `duplicate_reward_path`, `rescore_trigger`                                  |
| 4     | `context_state`  | `stale_context_conflict` — demotes an otherwise-allow to `hold`                                |

Composite decisions arise when more than one signal genuinely fires and neither dominates the rollback action — e.g. cooldown + rescore (hold and requeue), or replay + duplicate-reward (block and revert).

## Embedded fixtures

The module ships with **9 tasks**, **4 wallets**, **3 prior ledger events**, and **11 candidate cases**. Each case is hand-designed to exercise one branch of the engine.

### Tasks

| Task             | Sector       | Rubric  | Notable property                                  |
| ---------------- | ------------ | ------- | ------------------------------------------------- |
| TASK-AERO-001    | defi         | 5       | clean baseline                                    |
| TASK-AERO-002    | defi         | 5       | recent submission → cooldown active               |
| TASK-VVV-010     | ai_agent     | 5       | `reward_paid: true`                               |
| TASK-FIL-021     | depin        | 3       | rubric outdated → rescore                         |
| TASK-PENGU-033   | memecoin     | 5       | context snapshot too old                          |
| TASK-RENDER-044  | ai_compute   | 5       | has terminal `rewarded` event on ledger           |
| TASK-AR-055      | storage      | 5       | has terminal `rewarded` event on ledger           |
| TASK-TAO-066     | ai_agent     | 5       | `claimed_by: rWALLET_ALPHA` — drives ownership case |
| TASK-OLAS-077    | ai_agent     | 4       | recent submission AND outdated rubric             |

### Wallets

| Wallet         | Authorized sectors                                  | Notable property                       |
| -------------- | --------------------------------------------------- | -------------------------------------- |
| rWALLET_ALPHA  | defi, ai_agent, ai_compute, storage, depin, memecoin | general-purpose, broadly authorized   |
| rWALLET_BETA   | defi, ai_agent, storage, depin                      | used for replay and ownership cases    |
| rWALLET_GAMMA  | defi only                                            | used to trigger unauthorized           |
| rWALLET_DELTA  | memecoin, defi                                       | recent context refresh exposes staleness |

### Cases

| #  | Case id                              | Decision  | Reason codes                                       | Source         |
| -- | ------------------------------------ | --------- | -------------------------------------------------- | -------------- |
| 1  | CASE-001-clean-allow                 | `allow`   | (none)                                             | task_state     |
| 2  | CASE-002-unauthorized                | `block`   | unauthorized_contributor                           | wallet_auth    |
| 3  | CASE-003-cooldown-bypass             | `hold`    | cooldown_bypass                                    | task_state     |
| 4  | CASE-004-duplicate-reward            | `block`   | duplicate_reward_path                              | event_history  |
| 5  | CASE-005-stale-context               | `hold`    | stale_context_conflict                             | context_state  |
| 6  | CASE-006-replayed-event              | `block`   | replayed_event + duplicate_reward_path             | composite      |
| 7  | CASE-007-rescore-trigger             | `rescore` | rescore_trigger                                    | task_state     |
| 8  | CASE-008-duplicate-event-reconcile   | `block`   | replayed_event + duplicate_reward_path             | composite      |
| 9  | CASE-009-composite                   | `hold`    | cooldown_bypass + rescore_trigger                  | composite      |
| 10 | CASE-010-stale-ownership             | `hold`    | stale_ownership_conflict                           | task_state     |
| 11 | CASE-011-unknown-task                | `block`   | unauthorized_contributor                           | wallet_auth    |

**Summary: 1 allow / 4 hold / 5 block / 1 rescore = 11 total.**

The `EXPECTED` dict at the bottom of the module encodes this contract; `--test` asserts byte-for-byte agreement and re-runs the engine to verify determinism.

## Example output

A single decision record looks like this (from `--pretty` mode):

```json
{
  "case_id": "CASE-008-duplicate-event-reconcile",
  "decision": "block",
  "reason_codes": [
    "duplicate_reward_path",
    "replayed_event"
  ],
  "authoritative_source": "composite",
  "reconciliation_status": "reconciled",
  "rollback_action": "revert_reward",
  "operator_note": "replayed (task,wallet,nonce) tuple already terminal as 'rewarded' at 1729700000; reward already issued - revert before re-evaluating"
}
```

The full output is wrapped in an envelope with config metadata and a summary count:

```json
{
  "active_rubric_version": 5,
  "cooldown_seconds": 3600,
  "context_ttl_seconds": 86400,
  "evaluated_at": 1730005000,
  "summary": { "total": 11, "allow": 1, "hold": 4, "block": 5, "rescore": 1 },
  "decisions": [ /* 11 records */ ]
}
```

## Determinism guarantees

- `NOW` is a frozen integer (`1_730_005_000`), not `time.time()`. Replace at module top to wire into a live clock.
- `EVENT_HISTORY` is static. The engine is a pure function over a snapshot — it does not mutate state.
- Reason codes are sorted in output; `--test` runs the engine twice and asserts identical JSON.
- All times are integer epoch seconds (no timezone parsing, no float drift).

## Extending

Common adaptations:

- **Different domain** — rename `WALLETS` → `CONTRIBUTORS`, `TASKS` → whatever your unit of work is. The engine doesn't care about the names.
- **Add a reason code** — append to the constants block, add to `REASON_CODES`, add a branch in `evaluate()`, add an `EXPECTED` entry for any new fixture that exercises it.
- **Change precedence** — the order is encoded in the structure of `evaluate()`. Reorder the branches; everything downstream still works.
- **Live state** — replace `NOW`, `TASKS`, `WALLETS`, `EVENT_HISTORY` with calls to your data layer. The decision schema and engine logic do not change.

## File layout

Single module, ~745 lines:

- Module docstring with schema, precedence rules, and reason-code reference
- Reason code, decision, source, and rollback-action constants
- `TASKS`, `WALLETS`, `EVENT_HISTORY` fixtures
- `CASES` list (11 entries)
- `Decision` dataclass
- `_find_event` and `_wallet_paid_task` helpers
- `evaluate(case)` — the engine
- `EXPECTED` contract dict
- `test_decisions()` — self-verification
- `run()` and `main()` — driver

## License

Public domain / unlicense — copy, modify, redistribute freely.
