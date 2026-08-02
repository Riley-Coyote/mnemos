# Host Mutation Protocol

Mnemos Core 0.3.1 exposes a small host-neutral API for adapters that persist an
event before delivery and may deliver it more than once. The first protocol
version covers the five public operations that mutate canonical memory state:

- `capture`
- `correct`
- `maintain`
- `reflect`
- `introduce`

Recall, context, and health are reads and do not use this contract.

## API

```python
from mnemos import MnemosRuntime

runtime = MnemosRuntime(
    db_path="/profile/mnemos/mnemos.db",
    agent_id="stable-agent-uuid",
    person_id="pseudonymous-person-id",
    project_scope="project-id",
    use_dedicated_model=False,
)

response = runtime.execute_host_mutation(
    "capture",
    {"content": "The release target is Hermes v2026.7.30."},
    host_namespace="mnemos-hermes/v1",
    idempotency_key="queue-event-018f...",
    protocol_version=1,
)
```

The response is JSON-compatible:

```json
{
  "protocol_version": 1,
  "operation": "capture",
  "idempotency_key": "queue-event-018f...",
  "replayed": false,
  "result": "Captured continuity. ..."
}
```

A retry with the same namespace, key, scope, operation, and arguments returns
the original `result` with `replayed: true`. A key reused with a different
request raises `HostMutationConflictError`. Unsupported protocol versions and
operations fail before any mutation. Requests and results are limited to 1 MiB.

## Atomicity boundary

Schema v8 adds `host_mutations`. Core begins an immediate SQLite transaction,
claims the `(host_namespace, idempotency_key)` pair, executes the operation,
stores its result, and commits all three together. If the process raises or is
killed before commit, SQLite rolls back both the partial Core effects and the
claim, so delivery can start again. Concurrent delivery waits on the SQLite
writer lock and then returns the committed result.

The request fingerprint includes protocol version, operation, arguments, and
the complete agent/person/project scope. This makes accidental cross-scope key
reuse a conflict rather than a disclosure or write in the wrong scope.

Canonical state in this guarantee includes the engram, FTS, connection,
version, archive, belief, functional-memory, hypomnema, reflection, identity,
meta, and consolidation tables in the Core database. Handoff behavior and its
schema-v7 fields are unchanged.

Embeddings are not canonical state. They are an optional, rebuildable cache
written through a separate SQLite connection, so host mutation execution
suppresses embedding reads and writes inside the atomic section. FTS and the
continuity layer remain immediately available. An adapter that enables semantic
embeddings may rebuild that cache after durable delivery.

Calls to an optional model during deep maintenance are also outside the durable
state guarantee. Their resulting Core writes are atomic, but an external model
request can be repeated after a process crash. Host adapters that require no
external repeat should use `maintain` with `deep=False`.

## Adapter rules

- Use a stable, adapter-specific `host_namespace`; include its compatibility
  version so future semantics cannot collide with old keys.
- Generate the idempotency key when the host event is persisted, not when it is
  delivered.
- Reuse that exact key for every retry and synchronous fast-path attempt.
- Do not reuse a key for another event, even in a different memory scope.
- Treat `HostMutationConflictError` as a dead letter requiring operator review.
- Keep automatic writes consent-gated in the adapter. This API guarantees
  replay safety; it does not grant permission to write.
