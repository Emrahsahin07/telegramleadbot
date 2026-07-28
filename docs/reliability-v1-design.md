# Reliability v1 technical design

Status: the fenced delivery outbox and minimal queue lease slice is implemented
in the current uncommitted `reliability-v1` worktree. Production rollout is not
authorized by this document. Typed AI outcomes and durable queue-level Telegram
identity remain proposed follow-up work.

## Implemented delivery semantics

Delivery is **durable at-least-once**, with:

- stable Telegram event identity `chat_id:message_id`;
- one durable row per `(event_id, recipient_uid)`;
- no send for a row already recorded as `delivered`;
- a random `lease_token` fencing every outbox attempt;
- bounded Telegram send duration shorter than the delivery lease;
- a finite retry budget and permanent `dead` state.

Exactly-once delivery is not claimed. A crash after Telegram accepts a message
but before SQLite records `delivered` can still produce a duplicate after
lease recovery. Telethon's high-level `send_message` creates its MTProto
`random_id` internally and does not expose a simple durable reuse mechanism
across process restarts. Reusing it would require replacing the current path
with raw MTProto requests and persisting additional request state. That change
is not justified for this reliability slice.

The current execution model has one sender source of truth: routing only
persists outbox rows, while the background outbox worker performs Telegram
sends. Routing reports `queued_uids`; it never reports them as delivered.

Queue and outbox claims use independent expiring leases and fencing tokens.
Legacy production rows already in `queue.status='processing'` receive nullable
lease columns during migration but are not automatically reclaimed because
their `lease_until` remains null. Queue lease/reclaim is enabled only in
outbox mode (`WRITE_OUTBOX=1`, `DELIVERY_OUTBOX_WORKER=1`); compatibility mode
`0/0` keeps legacy no-reclaim behavior.

## Goals

- Preserve every accepted Telegram event across worker/process failures.
- Retry transient AI and Telegram failures without duplicating already
  successful recipients.
- Keep the deployment to one Python process and one SQLite database on a small
  AWS server.
- Preserve current lead relevance, category, location, subscription, and
  threshold behavior during the reliability migration.

## Non-goals

- Changing AI prompts, thresholds, filters, categories, or location rules.
- Migrating subscriptions.
- Introducing Redis, Celery, PostgreSQL, Kafka, or another service.
- Reprocessing historical completed events automatically.

## Proposed event identity

Telegram event identity is `(chat_id, message_id)`. Store both as first-class
columns and enforce a database constraint:

```sql
CREATE UNIQUE INDEX uq_queue_chat_message
ON queue(chat_id, message_id);
```

Legacy JSON payloads must be backfilled before this constraint is created.
Rows whose payload cannot be parsed move to `dead` with a migration error; they
must not be silently deleted.

## Proposed queue schema

The existing `queue` table can be migrated in place:

```sql
ALTER TABLE queue ADD COLUMN chat_id INTEGER;
ALTER TABLE queue ADD COLUMN message_id INTEGER;
ALTER TABLE queue ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE queue ADD COLUMN next_attempt_at TEXT;
ALTER TABLE queue ADD COLUMN lease_until TEXT;
ALTER TABLE queue ADD COLUMN last_error TEXT;
ALTER TABLE queue ADD COLUMN outcome TEXT;
ALTER TABLE queue ADD COLUMN pipeline_version TEXT;
```

Allowed queue states:

- `pending`: available when `next_attempt_at` is null or due.
- `processing`: claimed by one worker until `lease_until`.
- `completed`: processing and durable recipient routing finished.
- `ignored`: valid business result that intentionally produces no delivery.
- `retry`: transient failure waiting for `next_attempt_at`.
- `dead`: payload or operation exhausted its retry budget.

`outcome` records a stable reason code without overloading `status`, for
example `not_relevant`, `no_subscribers`, `ai_timeout`, or `invalid_payload`.

## Queue claim and reclaim

Claim must be a single transaction using `UPDATE ... RETURNING`:

1. Select one `pending` or due `retry` row.
2. Set `status='processing'`.
3. Increment `attempts`.
4. Set `lease_until` to a short UTC deadline.
5. Return the payload to the worker.

On startup and periodically, expired `processing` rows are changed to `retry`
with an immediate `next_attempt_at`. Reclaim must not run until per-recipient
delivery idempotency is available.

Backoff should be bounded and deterministic with small jitter:

```text
delay = min(base * 2 ** (attempts - 1), max_delay)
```

After the configured attempt limit, transition to `dead` and alert the
administrator.

## Typed processing outcomes

Pipeline functions should return one of:

```text
completed  - classification/routing succeeded and outbox rows are durable
ignored    - intentional business drop with a reason code
retry      - transient AI, SQLite, Telegram, or network failure
dead       - invalid payload or exhausted retries
```

Technical AI failures must never be represented as `relevant=false`.

## Implemented delivery outbox

```sql
CREATE TABLE delivery_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    recipient_uid INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    lease_until TEXT,
    lease_token TEXT,
    telegram_message_id INTEGER,
    last_error TEXT,
    payload TEXT NOT NULL,
    payload_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at TEXT,
    UNIQUE(event_id, recipient_uid)
);

CREATE INDEX idx_delivery_ready
ON delivery_outbox(status, next_attempt_at, id);
```

Routing inserts one row per matching recipient with `INSERT ... ON CONFLICT DO
NOTHING`. Creation of all outbox rows and completion of the queue event happen
in one transaction. A delivery worker claims and retries recipients
independently. Every claim replaces `lease_token`; delivered/retry/dead
transitions require the same token and verify that exactly one row changed.

Permanent Telegram errors (`UserIsBlockedError`,
`InputUserDeactivatedError`, irrecoverable `PeerIdInvalidError`) transition
that recipient row to `dead` with a permanent reason. Flood wait, network,
timeout, and retryable RPC errors transition to `retry`.

## Idempotency semantics

- Event: exactly one queue row per `(chat_id, message_id)`.
- Recipient: exactly one outbox row per `(event_id, recipient_uid)`.
- A retry never calls recipients whose outbox row is already `delivered`.
- Telegram does not expose a general send-message idempotency key. A process
  crash after Telegram accepts the message but before SQLite commits
  `delivered` leaves a narrow duplicate window. Record the returned Telegram
  message ID immediately and keep messages deterministic. This limitation must
  be documented and monitored.

## Backward-compatible migration

1. Stop the service and take verified copies of queue DB, WAL/SHM files, and
   subscriptions.
2. Run `PRAGMA integrity_check`.
3. Add nullable columns only; do not change existing status behavior.
4. Backfill `chat_id/message_id` from `event` JSON in bounded transactions.
5. Report invalid and duplicate legacy rows without deleting them.
6. Resolve duplicates deterministically, retaining the earliest canonical row
   and recording duplicate mappings in a migration audit table.
7. Create the unique event index.
8. Create `delivery_outbox`.
9. Deploy code in compatibility mode: read old rows, write new columns/outbox.
10. Enable lease reclaim only after outbox delivery has been verified.
11. Keep old columns and statuses for at least one release.

The migration script must be idempotent and record its schema version in:

```sql
CREATE TABLE schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
```

## Rollback

Before enabling the new workers:

- Stop the service.
- Restore the verified pre-migration database and WAL/SHM set.
- Deploy the tagged pre-reliability code.
- Start exactly one service instance.

After new outbox deliveries have started, database rollback alone is unsafe
because delivered messages cannot be undone. Instead:

1. Stop claiming new events and outbox rows.
2. Preserve the migrated DB as an audit snapshot.
3. Export undelivered outbox rows.
4. Deploy compatibility code that can finish or explicitly quarantine them.
5. Only restore the old DB after reconciling which recipients were already
   delivered.

Feature flags should separate rollout:

```text
WRITE_OUTBOX=0/1
DELIVERY_OUTBOX_WORKER=0/1
```

Only `0/0` and `1/1` are valid. Rollback first disables outbox mode through
both flags; destructive schema downgrade is not part of the normal rollback
path.
