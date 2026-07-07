"""
SQL-file migration runner for the Mnemos SQLite store (v1 §14 step 0).

This is the upgrade to the shipped migration machinery. The Python-function
migrations in ``migrations.py`` (v2..v{SCHEMA_VERSION}) stay frozen as history
for virgin-store builds; every NEW migration is an additive-only SQL file under
``migrations/NNNN_name.sql`` and is applied through :class:`MigrationRunner`.

The contract lives in ``pai-supervision/specs/migration-runner-spec-2026-07-07.md``
(RATIFIED, DAVID-25). Load-bearing invariants, restated so the next reader does
not have to leave the file:

- **Additive-only shadow DDL.** New tables + nullable/constant-default columns
  only. The statement lint (§2.1) is the mechanical gate — the ``additive-only``
  attestation line is checked, but the lint refuses the run regardless of what
  the attestation claims. ``CREATE TRIGGER``, ``PRAGMA`` writes, ``VACUUM``,
  ``ATTACH``/``DETACH``, and every destructive statement abort with the
  offending statement named.
- **Snapshot before every apply, faithful to true pre-migration state.** The
  SQLite backup API (never a file copy) writes the snapshot; ``PRAGMA
  data_version`` is read before the snapshot and re-read after ``BEGIN
  IMMEDIATE`` holds the write lock — if a writer landed in the window the
  snapshot is stale and is re-taken.
- **One transaction per version.** ``BEGIN IMMEDIATE`` wraps the SQL + the
  ``schema_migrations`` row; a mid-migration crash rolls back to a clean
  pre-migration state. No partially-applied version can exist.
- **No down-migrations.** Rollback is snapshot restore. Checksum mismatch on an
  already-applied version is an incident abort, not a retry.
- **Schema, never meaning.** The runner does no seeding/backfills/data rewrites,
  and refuses to be pointed at a non-canonical store (§6 — enforced at the CLI).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


# ── Statement lint (§2.1) ──────────────────────────────────────────────────

# The allowlist is expressed as (name, matcher) so an abort can name WHICH
# class a statement failed to match, and so CREATE TABLE/INDEX/VIEW are told
# apart from CREATE TRIGGER by the second keyword rather than a bare prefix.
_WS = r"[\s]+"

_ALLOWLIST: tuple[tuple[str, re.Pattern[str]], ...] = (
    # New tables only. IF NOT EXISTS permitted. CREATE TEMP/TEMPORARY excluded.
    ("CREATE TABLE", re.compile(r"^CREATE" + _WS + r"TABLE(?!\s+TEMP)", re.IGNORECASE)),
    # ALTER TABLE ... ADD COLUMN only. RENAME/DROP COLUMN excluded (see below).
    (
        "ALTER TABLE ADD COLUMN",
        re.compile(
            r"^ALTER" + _WS + r"TABLE" + _WS + r".+?" + _WS + r"ADD" + _WS
            + r"(COLUMN" + _WS + r")?",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    ("CREATE INDEX", re.compile(r"^CREATE" + _WS + r"INDEX", re.IGNORECASE)),
    (
        "CREATE UNIQUE INDEX",
        re.compile(r"^CREATE" + _WS + r"UNIQUE" + _WS + r"INDEX", re.IGNORECASE),
    ),
    ("CREATE VIEW", re.compile(r"^CREATE" + _WS + r"VIEW", re.IGNORECASE)),
)


@dataclass(frozen=True)
class _SqlToken:
    kind: str
    value: str


@dataclass(frozen=True)
class _DefaultLiteral:
    end: int
    is_null: bool


class MigrationLintError(RuntimeError):
    """A migration statement failed the §2.1 allowlist. The message names the
    offending statement and the reason so the abort is legible at the CLI."""


class MigrationError(RuntimeError):
    """Apply-time failure: SQL error, checksum incident, snapshot failure, or a
    version-table inconsistency. Distinct from lint so callers can tell a
    malformed migration file from a runtime abort."""


def split_statements(sql_text: str) -> list[str]:
    """Split a SQL script into statements on top-level semicolons.

    Semicolons inside single/double-quoted string literals do not terminate a
    statement. This is a lexer, not a full parser — the allowlist (below) is
    what decides whether the resulting statement is permitted, so the split only
    has to avoid the string-literal trap, not understand SQL grammar.
    """
    statements: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(sql_text)
    while i < n:
        ch = sql_text[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                # SQL escapes a quote by doubling it.
                if i + 1 < n and sql_text[i + 1] == quote:
                    buf.append(sql_text[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql_text[i + 1] == "-":
            if buf and not buf[-1].isspace():
                buf.append(" ")
            i += 2
            while i < n and sql_text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and sql_text[i + 1] == "*":
            if buf and not buf[-1].isspace():
                buf.append(" ")
            i += 2
            while i + 1 < n and not (sql_text[i] == "*" and sql_text[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _outside_single_quotes_upper(statement: str) -> str:
    chars: list[str] = []
    quote = False
    i = 0
    n = len(statement)
    while i < n:
        ch = statement[i]
        if quote:
            if ch == "'":
                if i + 1 < n and statement[i + 1] == "'":
                    i += 2
                    continue
                quote = False
            i += 1
            continue
        if ch == "'":
            quote = True
            i += 1
            continue
        chars.append(ch)
        i += 1
    return " ".join("".join(chars).split()).upper()


def _sql_tokens(statement: str) -> list[_SqlToken]:
    tokens: list[_SqlToken] = []
    i = 0
    n = len(statement)
    while i < n:
        ch = statement[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "'":
            value: list[str] = []
            i += 1
            while i < n:
                if statement[i] == "'":
                    if i + 1 < n and statement[i + 1] == "'":
                        value.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                value.append(statement[i])
                i += 1
            tokens.append(_SqlToken("string", "".join(value).upper()))
            continue
        if ch in ('"', "`"):
            quote = ch
            value = []
            i += 1
            while i < n:
                if statement[i] == quote:
                    if i + 1 < n and statement[i + 1] == quote:
                        value.append(quote)
                        i += 2
                        continue
                    i += 1
                    break
                value.append(statement[i])
                i += 1
            tokens.append(_SqlToken("identifier", "".join(value).upper()))
            continue
        if ch == "[":
            value = []
            i += 1
            while i < n and statement[i] != "]":
                value.append(statement[i])
                i += 1
            i = min(i + 1, n)
            tokens.append(_SqlToken("identifier", "".join(value).upper()))
            continue
        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < n and (statement[i].isalnum() or statement[i] in "_$"):
                i += 1
            tokens.append(_SqlToken("word", statement[start:i].upper()))
            continue
        if ch.isdigit():
            start = i
            i += 1
            while i < n and (statement[i].isalnum() or statement[i] in "._$"):
                i += 1
            tokens.append(_SqlToken("number", statement[start:i].upper()))
            continue
        tokens.append(_SqlToken("symbol", ch))
        i += 1
    return tokens


def _is_name_token(token: _SqlToken) -> bool:
    return token.kind in {"word", "identifier", "string", "number"}


def _consume_qualified_name(tokens: list[_SqlToken], index: int) -> int | None:
    if index >= len(tokens) or not _is_name_token(tokens[index]):
        return None
    index += 1
    if index < len(tokens) and tokens[index].value == ".":
        index += 1
        if index >= len(tokens) or not _is_name_token(tokens[index]):
            return None
        index += 1
    return index


def _qualified_name_at(
    tokens: list[_SqlToken], index: int
) -> tuple[list[_SqlToken], int] | None:
    if index >= len(tokens) or not _is_name_token(tokens[index]):
        return None
    parts = [tokens[index]]
    end = index + 1
    if end < len(tokens) and tokens[end].value == ".":
        if end + 1 >= len(tokens) or not _is_name_token(tokens[end + 1]):
            return None
        parts.append(tokens[end + 1])
        end += 2
    return parts, end


def _is_create_table_as_select(statement: str) -> bool:
    tokens = _sql_tokens(statement)
    values = [token.value for token in tokens]
    if len(values) < 2 or values[0:2] != ["CREATE", "TABLE"]:
        return False
    index = 2
    if values[index : index + 3] == ["IF", "NOT", "EXISTS"]:
        index += 3
    name_end = _consume_qualified_name(tokens, index)
    if name_end is None:
        return False
    return (
        name_end + 1 < len(values)
        and values[name_end] == "AS"
        and values[name_end + 1] in {"SELECT", "WITH", "VALUES"}
    )


def _consume_constant_default_literal(
    tokens: list[_SqlToken], index: int
) -> _DefaultLiteral | None:
    if index >= len(tokens):
        return None
    token = tokens[index]
    if token.value in {"+", "-"}:
        if index + 1 < len(tokens) and tokens[index + 1].kind == "number":
            return _DefaultLiteral(index + 2, False)
        return None
    if token.kind in {"number", "string"}:
        return _DefaultLiteral(index + 1, False)
    if token.kind == "word" and token.value == "NULL":
        return _DefaultLiteral(index + 1, True)
    if token.kind == "word" and token.value in {"TRUE", "FALSE"}:
        return _DefaultLiteral(index + 1, False)
    if (
        token.kind == "word"
        and token.value == "X"
        and index + 1 < len(tokens)
        and tokens[index + 1].kind == "string"
    ):
        return _DefaultLiteral(index + 2, False)
    return None


def _is_column_constraint_boundary(token: _SqlToken) -> bool:
    return token.kind == "word" and token.value in {
        "CHECK",
        "COLLATE",
        "CONSTRAINT",
        "NOT",
        "NULL",
        "PRIMARY",
        "REFERENCES",
        "UNIQUE",
    }


def _validate_add_column_shape(statement: str, normalized: str) -> None:
    tokens = _sql_tokens(statement)
    values = [token.value for token in tokens]
    if len(values) < 4 or values[0:2] != ["ALTER", "TABLE"]:
        raise MigrationLintError(
            "banned statement: ALTER TABLE ADD COLUMN could not be parsed. "
            "Offending statement: " + normalized[:200]
        )
    table_end = _consume_qualified_name(tokens, 2)
    if table_end is None or table_end >= len(tokens) or values[table_end] != "ADD":
        raise MigrationLintError(
            "banned statement: ALTER TABLE ADD COLUMN could not be parsed. "
            "Offending statement: " + normalized[:200]
        )
    column_index = table_end + 1
    if column_index < len(tokens) and values[column_index] == "COLUMN":
        column_index += 1
    if column_index >= len(tokens) or not _is_name_token(tokens[column_index]):
        raise MigrationLintError(
            "banned statement: ALTER TABLE ADD COLUMN is missing a column name. "
            "Offending statement: " + normalized[:200]
        )

    definition = tokens[column_index + 1 :]
    has_default = False
    default_is_null = False
    has_not_null = False
    has_references = False
    for index, token in enumerate(definition):
        next_value = definition[index + 1].value if index + 1 < len(definition) else ""
        if token.kind == "word" and token.value == "GENERATED":
            raise MigrationLintError(
                "banned statement: ALTER TABLE ADD COLUMN generated columns are "
                "not additive-only shadow DDL. Offending statement: "
                + normalized[:200]
            )
        if token.kind == "word" and token.value == "AS":
            raise MigrationLintError(
                "banned statement: ALTER TABLE ADD COLUMN AS/generated columns "
                "are not additive-only shadow DDL. Offending statement: "
                + normalized[:200]
            )
        if token.kind == "word" and token.value in {"PRIMARY", "UNIQUE"}:
            raise MigrationLintError(
                "banned statement: ALTER TABLE ADD COLUMN PRIMARY KEY/UNIQUE "
                "constraints are not additive-only shadow DDL. Offending "
                "statement: " + normalized[:200]
            )
        if token.kind == "word" and token.value == "REFERENCES":
            has_references = True
        if token.kind == "word" and token.value == "NOT" and next_value == "NULL":
            has_not_null = True
        if token.kind == "word" and token.value == "DEFAULT":
            if has_default:
                raise MigrationLintError(
                    "banned statement: ALTER TABLE ADD COLUMN has multiple "
                    "DEFAULT clauses. Offending statement: " + normalized[:200]
                )
            has_default = True
            default = _consume_constant_default_literal(definition, index + 1)
            if default is None or (
                default.end < len(definition)
                and not _is_column_constraint_boundary(definition[default.end])
            ):
                raise MigrationLintError(
                    "banned statement: ALTER TABLE ADD COLUMN DEFAULT must be a "
                    "constant literal. Offending statement: "
                    + normalized[:200]
                )
            default_is_null = default.is_null
    if has_references and has_default and not default_is_null:
        raise MigrationLintError(
            "banned statement: ALTER TABLE ADD COLUMN REFERENCES requires a NULL "
            "DEFAULT. Offending statement: " + normalized[:200]
        )
    if has_not_null and (not has_default or default_is_null):
        raise MigrationLintError(
            "banned statement: ALTER TABLE ADD COLUMN NOT NULL requires a "
            "non-NULL constant DEFAULT. Offending statement: " + normalized[:200]
        )


def _references_schema_migrations(statement: str) -> bool:
    tokens = _sql_tokens(statement)
    table_reference_context = {
        "TABLE",
        "INTO",
        "UPDATE",
        "FROM",
        "JOIN",
        "ON",
        "REFERENCES",
    }
    for index, token in enumerate(tokens):
        parsed = _qualified_name_at(tokens, index)
        if parsed is None:
            continue
        parts, _ = parsed
        if parts[-1].value != "SCHEMA_MIGRATIONS":
            continue
        previous = tokens[index - 1].value if index > 0 else ""
        if previous in table_reference_context:
            return True
        if len(parts) > 1:
            return True
        if any(part.kind in {"word", "identifier"} for part in parts):
            return True
    return False


def classify_statement(statement: str) -> str:
    """Return the allowlist class name for a statement, or raise
    :class:`MigrationLintError` naming the offending statement.

    The lint is an allowlist, not a denylist: anything that does not match a
    permitted class aborts — so a novel destructive construct we did not
    enumerate still fails closed. The three named exclusions (CREATE TRIGGER,
    PRAGMA, VACUUM/ATTACH/DETACH) get specific messages because they are the
    ones a well-meaning author is most likely to reach for.
    """
    normalized = " ".join(statement.split())
    upper = normalized.upper()

    # Named exclusions with reasons (§2.1). Checked before the allowlist so the
    # message explains WHY, not just "not allowed".
    if re.match(r"^CREATE" + _WS + r"(TEMP\w*\s+)?TRIGGER\b", upper):
        raise MigrationLintError(
            "banned statement: CREATE TRIGGER mutates data on write (meaning, "
            "not schema); §6 rules it out of migrations. Offending statement: "
            f"{normalized[:200]}"
        )
    if re.match(r"^PRAGMA\b", upper):
        raise MigrationLintError(
            "banned statement: PRAGMA write is non-transactional and voids the "
            "§3 crash guarantee. Offending statement: " + normalized[:200]
        )
    if re.match(r"^VACUUM\b", upper):
        raise MigrationLintError(
            "banned statement: VACUUM cannot run inside a transaction. "
            "Offending statement: " + normalized[:200]
        )
    if re.match(r"^ATTACH\b", upper) or re.match(r"^DETACH\b", upper):
        raise MigrationLintError(
            "banned statement: ATTACH/DETACH opens a second store, which the "
            "one-store invariant (§6) forbids. Offending statement: "
            + normalized[:200]
        )
    if _references_schema_migrations(statement):
        raise MigrationLintError(
            "banned statement: schema_migrations is runner-owned; migration "
            "files may not touch it. Offending statement: " + normalized[:200]
        )
    if _is_create_table_as_select(statement):
        raise MigrationLintError(
            "banned statement: CREATE TABLE AS SELECT materializes data from "
            "existing tables (meaning, not schema). Offending statement: "
            + normalized[:200]
        )

    for name, pattern in _ALLOWLIST:
        if pattern.match(normalized):
            # ALTER ... ADD COLUMN: reject an ADD that is not a column add
            # (e.g. ADD CONSTRAINT is not valid SQLite, but be explicit).
            if name == "ALTER TABLE ADD COLUMN":
                _validate_add_column_shape(statement, normalized)
                return name
            return name

    raise MigrationLintError(
        "statement not on the additive-only allowlist (destructive or "
        "meaning-changing DDL/DML aborts the whole run). Offending statement: "
        f"{normalized[:200]}"
    )


def lint_migration_sql(sql_text: str) -> list[str]:
    """Lint every statement in a migration file. Returns the list of statement
    classes on success; raises :class:`MigrationLintError` on the first
    offending statement. The attestation line is NOT consulted here — the lint
    is the gate."""
    classes: list[str] = []
    for statement in split_statements(sql_text):
        classes.append(classify_statement(statement))
    return classes


# ── Migration files ────────────────────────────────────────────────────────

_FILENAME_RE = re.compile(r"^(\d{4})_([A-Za-z0-9][A-Za-z0-9_\-]*)\.sql$")
_ATTESTATION_RE = re.compile(r"--\s*additive-only:\s*yes\b", re.IGNORECASE)


@dataclass(frozen=True)
class MigrationFile:
    version: int
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    def has_attestation(self) -> bool:
        return bool(_ATTESTATION_RE.search(self.sql))


def discover_migration_files(migrations_dir: Path) -> list[MigrationFile]:
    """Load and version-sort ``NNNN_name.sql`` files.

    **Duplicate version numbers abort before anything runs** (§2) — two files
    claiming NNNN is the multi-seat collision case, and the runner refuses the
    whole set, not just the duplicates.
    """
    migrations_dir = Path(migrations_dir)
    if not migrations_dir.exists():
        return []
    by_version: dict[int, MigrationFile] = {}
    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file() or path.suffix != ".sql":
            continue
        m = _FILENAME_RE.match(path.name)
        if not m:
            raise MigrationError(
                f"migration filename does not match NNNN_name.sql: {path.name}"
            )
        version = int(m.group(1))
        name = m.group(2)
        if version in by_version:
            other = by_version[version].path.name
            raise MigrationError(
                f"duplicate migration version {version}: {path.name} and "
                f"{other} both claim it; refusing the whole set (§2 "
                "multi-seat collision guard)"
            )
        sql = path.read_text(encoding="utf-8")
        by_version[version] = MigrationFile(
            version=version, name=name, path=path, sql=sql
        )
    return [by_version[v] for v in sorted(by_version)]


# ── Snapshots (§5) ─────────────────────────────────────────────────────────

def snapshot_db(source_db: Path, dest_db: Path) -> Path:
    """Write an integrity-checked snapshot via the SQLite backup API.

    Never a file copy — the WAL/SHM companions would be missed and the snapshot
    would be an unfaithful rollback primitive (§5, v1 §13.2). Mirrors the
    hardening in ``migrations.backup_sqlite_db`` but is kept local so the runner
    does not depend on the importer's helper.
    """
    source = Path(source_db).expanduser()
    dest = Path(dest_db).expanduser()
    if not source.exists():
        raise FileNotFoundError(source)
    if source.resolve() == dest.resolve():
        raise ValueError("snapshot destination must differ from source")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.tmp")
    if tmp.exists():
        tmp.unlink()

    source_conn = sqlite3.connect(str(source))
    dest_conn = sqlite3.connect(str(tmp))
    committed = False
    try:
        source_conn.backup(dest_conn)
        result = dest_conn.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise MigrationError(f"snapshot integrity check failed: {result}")
        dest_conn.commit()
        committed = True
    finally:
        dest_conn.close()
        source_conn.close()
        if not committed and tmp.exists():
            tmp.unlink()

    tmp.replace(dest)
    return dest


def _read_data_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA data_version").fetchone()[0])


# ── Plan / apply results ───────────────────────────────────────────────────

@dataclass
class PlannedMigration:
    version: int
    name: str
    checksum: str
    statement_classes: list[str]
    snapshot_path: str
    has_attestation: bool


@dataclass
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: str
    snapshot_path: str


@dataclass
class PlanResult:
    current_version: int
    pending: list[PlannedMigration] = field(default_factory=list)


# ── The runner ─────────────────────────────────────────────────────────────

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TEXT NOT NULL,
    snapshot    TEXT NOT NULL
)
"""

GRANDFATHERED_CHECKSUM = "grandfathered"
GRANDFATHERED_SNAPSHOT = "pre-spec"


def default_migrations_dir() -> Path:
    """The shipped SQL migrations directory (``mnemos/store/migrations``)."""
    return Path(__file__).resolve().parent / "migrations"


class MigrationRunner:
    """Applies additive-only SQL-file migrations under the §3 apply contract.

    The runner owns its own connection to ``db_path``. It NEVER accepts a live
    ``EngramStore`` connection or a second store — §6's one-store invariant is
    enforced by the caller (the CLI resolves the canonical DB and refuses a path
    argument); the runner refuses only what it can see: a nonexistent DB.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        migrations_dir: Path | None = None,
        snapshot_root: Path | None = None,
        known_python_versions: list[int] | None = None,
    ):
        self.db_path = Path(db_path).expanduser()
        self.migrations_dir = Path(
            migrations_dir if migrations_dir is not None else default_migrations_dir()
        )
        self.snapshot_root = Path(
            snapshot_root
            if snapshot_root is not None
            else self.db_path.parent / "backups" / "migrations"
        )
        # Versions the frozen Python migrations own (v2..SCHEMA_VERSION). Used
        # to backfill grandfathered schema_migrations rows and to compute the
        # binary's known-max version for the fail-closed check.
        self._known_python_versions = sorted(known_python_versions or [])
        # Receipts queued before the receipts journal exists (§3 bootstrap
        # rule). Drained after the journal-creating version commits; today the
        # journal organ does not exist yet, so these surface as the return
        # value / a log line and the schema_migrations row is the receipt of
        # record.
        self.pending_receipts: list[dict] = []

    # -- helpers ------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _connect_readonly(self) -> sqlite3.Connection:
        uri = f"file:{quote(str(self.db_path.resolve()), safe='/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_version_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(_SCHEMA_MIGRATIONS_DDL)

    def _has_table(self, conn: sqlite3.Connection, name: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
            is not None
        )

    def _read_applied_versions(self, conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
        if not self._has_table(conn, "schema_migrations"):
            return {}
        return {
            int(r["version"]): r
            for r in conn.execute(
                "SELECT version, name, checksum, applied_at, snapshot "
                "FROM schema_migrations"
            ).fetchall()
        }

    def _read_meta_schema_version(self, conn: sqlite3.Connection) -> int:
        if not self._has_table(conn, "meta"):
            return 0
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            return 0
        try:
            version = int(row[0])
        except (TypeError, ValueError):
            raise RuntimeError(f"Malformed schema_version: {row[0]!r}")
        if version < 0:
            raise RuntimeError(f"Malformed schema_version: {row[0]!r}")
        return version

    def _backfill_grandfathered(self, conn: sqlite3.Connection) -> None:
        """Populate schema_migrations for versions that ran before this spec.

        The table must not lie: pre-spec versions carry ``checksum=
        'grandfathered'`` and ``snapshot='pre-spec'`` because they ran without
        the lint/snapshot contract. Backfills only the versions the store has
        actually reached (meta.schema_version), never versions ahead of it.
        """
        from .migrations import get_current_version

        reached = get_current_version(conn)
        existing = {
            int(r[0])
            for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        now = _utc_now()
        for version in self._known_python_versions:
            if version > reached:
                continue
            if version in existing:
                continue
            conn.execute(
                "INSERT INTO schema_migrations "
                "(version, name, checksum, applied_at, snapshot) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    version,
                    f"grandfathered_v{version}",
                    GRANDFATHERED_CHECKSUM,
                    now,
                    GRANDFATHERED_SNAPSHOT,
                ),
            )

    def applied_versions(self, conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
        self._ensure_version_table(conn)
        return self._read_applied_versions(conn)

    def current_version(self, conn: sqlite3.Connection) -> int:
        rows = self.applied_versions(conn)
        return max(rows) if rows else 0

    def known_max_version(self) -> int:
        """Highest version this binary understands: the max of the frozen
        Python versions and the shipped SQL-file versions."""
        sql_versions = [m.version for m in self._discover_sql_migration_files()]
        candidates = list(self._known_python_versions) + sql_versions
        return max(candidates) if candidates else 0

    def known_python_max_version(self) -> int:
        return max(self._known_python_versions) if self._known_python_versions else 0

    def _discover_sql_migration_files(self) -> list[MigrationFile]:
        files = discover_migration_files(self.migrations_dir)
        python_max = self.known_python_max_version()
        collisions = [m for m in files if m.version <= python_max]
        if collisions:
            names = ", ".join(m.path.name for m in collisions)
            raise MigrationError(
                f"SQL migration version collides with frozen Python migration "
                f"history: {names}; SQL-file migrations must start above "
                f"Python schema max {python_max}."
            )
        return files

    def check_meta_not_ahead(self, conn: sqlite3.Connection) -> int:
        meta_version = self._read_meta_schema_version(conn)
        binary_python_max = self.known_python_max_version()
        if meta_version > binary_python_max:
            raise MigrationError(
                f"meta.schema_version {meta_version} is newer than this binary's "
                f"frozen Python schema max ({binary_python_max}); refusing to run. "
                "A newer schema than the code means stop, not guess."
            )
        return meta_version

    def check_not_ahead(self, conn: sqlite3.Connection) -> None:
        """Fail closed if the store carries a schema_migrations version newer
        than this binary understands (§1). A newer schema than the code means
        stop, not guess."""
        rows = self._read_applied_versions(conn)
        if not rows:
            return
        db_max = max(rows)
        binary_max = self.known_max_version()
        if db_max > binary_max:
            raise MigrationError(
                f"schema_migrations version {db_max} is newer than this "
                f"binary understands ({binary_max}); refusing to run. A newer "
                "schema than the code means stop, not guess."
            )

    # -- plan (§7) ----------------------------------------------------------

    def plan(self) -> PlanResult:
        """Dry-run: pending versions, parsed statement classes, checksums, and
        the snapshot path each would create. Touches no schema — read only."""
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"migration plan requires an existing store: {self.db_path}"
            )
        conn = self._connect_readonly()
        try:
            meta_version = self.check_meta_not_ahead(conn)
            self.check_not_ahead(conn)
            applied = self._read_applied_versions(conn)
            current = max(
                max(applied) if applied else 0,
                meta_version,
            )
            files = self._discover_sql_migration_files()
            self._verify_applied_checksums(files, applied)
            result = PlanResult(current_version=current)
            for mig in files:
                if mig.version <= current:
                    continue
                # Lint at plan time too — plan must surface a bad migration
                # before apply is ever run. A lint failure here raises, which
                # is the right behavior: `plan` is what David reads before
                # `apply`, so a poisoned migration must be loud here.
                classes = lint_migration_sql(mig.sql)
                result.pending.append(
                    PlannedMigration(
                        version=mig.version,
                        name=mig.name,
                        checksum=mig.checksum,
                        statement_classes=classes,
                        snapshot_path=str(self._snapshot_path(mig.version)),
                        has_attestation=mig.has_attestation(),
                    )
                )
            return result
        finally:
            conn.close()

    # -- apply (§3) ---------------------------------------------------------

    def apply(self, target_version: int | None = None) -> list[AppliedMigration]:
        """Apply pending SQL-file migrations in ascending order under the §3
        contract. Idempotent: applied versions are skipped; a checksum mismatch
        on an applied version aborts as a named incident."""
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"migration runner requires an existing store: {self.db_path}"
            )
        conn = self._connect()
        applied_out: list[AppliedMigration] = []
        try:
            self.check_meta_not_ahead(conn)
            self._ensure_version_table(conn)
            self.check_not_ahead(conn)
            self._backfill_grandfathered(conn)
            conn.commit()

            files = self._discover_sql_migration_files()
            applied = self.applied_versions(conn)
            self._verify_applied_checksums(files, applied)

            current = max(applied) if applied else 0
            for mig in files:
                if mig.version <= current:
                    continue
                if target_version is not None and mig.version > target_version:
                    break
                applied_out.append(self._apply_one(conn, mig))
                current = mig.version
            return applied_out
        finally:
            conn.close()

    def _verify_applied_checksums(
        self, files: list[MigrationFile], applied: dict[int, sqlite3.Row]
    ) -> None:
        """A checksum mismatch on an already-applied SQL version is an incident
        (someone edited shipped history), not a retry. Grandfathered rows carry
        a sentinel checksum and are exempt — no file backs them."""
        by_version = {m.version: m for m in files}
        for version, row in applied.items():
            recorded = row["checksum"]
            if recorded == GRANDFATHERED_CHECKSUM:
                continue
            mig = by_version.get(version)
            if mig is None:
                # Applied SQL version whose file vanished — also an incident.
                raise MigrationError(
                    f"schema_migrations records version {version} but no "
                    f"migration file backs it; edited history is an incident, "
                    "not a retry."
                )
            if mig.checksum != recorded:
                raise MigrationError(
                    f"checksum mismatch on applied version {version} "
                    f"({mig.path.name}): recorded {recorded[:12]}…, file "
                    f"{mig.checksum[:12]}…. A shipped migration was edited; "
                    "this is an incident, not a retry."
                )

    def _snapshot_path(self, version: int) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return self.snapshot_root / f"v{version:04d}-{stamp}" / self.db_path.name

    def _apply_one(
        self, conn: sqlite3.Connection, mig: MigrationFile
    ) -> AppliedMigration:
        # 1. Lint — no pass, no apply.
        classes = lint_migration_sql(mig.sql)

        # 2-5. Snapshot, take the write lock, then write SQL + version row.
        try:
            snapshot_path = self._snapshot_or_resnapshot(conn, mig.version)
            for statement in split_statements(mig.sql):
                conn.execute(statement)
            applied_at = _utc_now()
            conn.execute(
                "INSERT INTO schema_migrations "
                "(version, name, checksum, applied_at, snapshot) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    mig.version,
                    mig.name,
                    mig.checksum,
                    applied_at,
                    str(snapshot_path),
                ),
            )
            conn.commit()
        except MigrationError:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised as MigrationError
            conn.rollback()
            raise MigrationError(
                f"migration {mig.version} ({mig.path.name}) failed and rolled "
                f"back to the pre-migration state: {exc}"
            ) from exc

        # 6. Receipt (§3 step 6 + bootstrap rule). Queue a structured receipt;
        #    then, if the migration_receipts journal exists (created by the
        #    journal-creating migration itself), drain the queue into it. For
        #    the journal-creating version, the table did not exist when this
        #    version's SQL ran inside the transaction above, so its receipt
        #    stays queued and the schema_migrations row is the receipt of
        #    record — exactly the bootstrap rule. The NEXT version drains it.
        receipt = {
            "kind": "migration-applied",
            "version": mig.version,
            "name": mig.name,
            "checksum": mig.checksum,
            "snapshot": str(snapshot_path),
            "applied_at": applied_at,
            "statement_classes": classes,
        }
        self.pending_receipts.append(receipt)
        self._drain_receipts(conn)
        return AppliedMigration(
            version=mig.version,
            name=mig.name,
            checksum=mig.checksum,
            applied_at=applied_at,
            snapshot_path=str(snapshot_path),
        )

    def _drain_receipts(self, conn: sqlite3.Connection) -> None:
        """Write queued receipts into the migration_receipts journal if it
        exists. No-op (receipts stay queued) while the journal is absent — the
        §3 bootstrap rule. Each drained receipt is removed from the queue; a
        receipt is written at most once (INSERT into an AUTOINCREMENT table)."""
        has_journal = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='migration_receipts'"
        ).fetchone()
        if not has_journal:
            return
        remaining: list[dict] = []
        try:
            for receipt in self.pending_receipts:
                conn.execute(
                    "INSERT INTO migration_receipts "
                    "(version, name, checksum, snapshot, applied_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        receipt["version"],
                        receipt["name"],
                        receipt["checksum"],
                        receipt["snapshot"],
                        receipt.get("applied_at", _utc_now()),
                    ),
                )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            # Leave receipts queued rather than lose them; the schema_migrations
            # rows remain the receipt of record.
            remaining = list(self.pending_receipts)
        self.pending_receipts = remaining

    def _snapshot_or_resnapshot(
        self, conn: sqlite3.Connection, version: int, max_attempts: int = 5
    ) -> Path:
        """Snapshot, begin the migration transaction, then accept only if
        data_version stayed stable while the write lock was acquired."""
        for _ in range(max_attempts):
            before = _read_data_version(conn)
            dest = self._snapshot_path(version)
            snapshot_db(self.db_path, dest)
            conn.execute("BEGIN IMMEDIATE")
            after = _read_data_version(conn)
            if before == after:
                return dest
            conn.rollback()
            try:
                dest.unlink()
                dest.parent.rmdir()
            except OSError:
                pass
        raise MigrationError(
            f"could not obtain a faithful snapshot for version {version}: "
            f"data_version kept moving across {max_attempts} attempts (a "
            "writer is active — stop writers before migrating)."
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
