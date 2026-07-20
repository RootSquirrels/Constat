"""Chantier IV.2 — migration-chain proof against a live Postgres.

Makes "the Alembic squash/baseline lost a column" structurally impossible
by pinning both ends of the migration chain to the ORM (the schema source
of truth, ADR-17):

A. **Fresh install ≡ ORM** (`test_fresh_install_matches_orm`): on a virgin
   schema, `alembic upgrade head` must produce exactly the schema
   `Base.metadata` declares — every table, every column, nullability,
   normalized types, and no undocumented extra tables/columns.

B. **Chain continuity** (`test_presquash_dump_reaches_same_head`): a
   database created by the pre-squash SQL dump (`db/migrations/_archived/
   0001..0021`, the state existing deployments are actually at), stamped
   at the baseline revision and upgraded to head, must reach the same
   schema as a fresh install — no column lost at the seam.

Both proofs share one differ (`diff_schema_against_metadata`). The
allow-lists (`EXTRA_TABLES_ALLOWLIST`, `EXTRA_COLUMNS_ALLOWLIST`) are the
only escape hatch: `alembic_version` is Alembic's own bookkeeping, and
the column allow-list starts EMPTY — if the chain ever loses or gains a
column the ORM doesn't know about, these tests fail and the drift is
fixed in the migration or the ORM, never silently allow-listed away.

These tests are `@pytest.mark.postgres`: they need a live database
(`CONSTAT_TEST_DATABASE_URL`, psycopg driver) and skip cleanly without
one. CI's postgres-rls job runs them against a postgres:16 service.
The type-token unit tests at the bottom need no database.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import (
    CHAR as SA_CHAR,
)
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    create_engine,
    inspect,
)

from tests.test_rls import (
    DATABASE_URL,
    MIGRATIONS_DIR,
    _apply_alembic_schema,
    _psycopg,
    requires_postgres,
)

ARCHIVED_MIGRATIONS_DIR = MIGRATIONS_DIR / "_archived"
ALEMBIC_INI = MIGRATIONS_DIR.parent / "alembic.ini"

# Tables allowed in the database beyond the ORM's. `alembic_version` is
# Alembic's own bookkeeping; the audit triggers (migration 0014) are
# functions, not tables, and never appear here.
EXTRA_TABLES_ALLOWLIST: frozenset[str] = frozenset({"alembic_version"})

# Database columns allowed beyond the ORM's, per table. MUST STAY EMPTY:
# a column the ORM doesn't declare is drift (today's known case —
# collect_jobs.enqueue_error / evaluation_status from migration 0021 —
# must be fixed by mapping the columns in the ORM, not by allow-listing).
EXTRA_COLUMNS_ALLOWLIST: dict[str, frozenset[str]] = {}


# ---------------------------------------------------------------------------
# Type normalization: ORM types and reflected PG types -> comparable tokens
# ---------------------------------------------------------------------------
#
# Both sides are normalized to a small token language so the comparison is
# explicit about what "equivalent" means:
#   - the custom decorators resolve to their PG native type (GUID -> uuid,
#     JSONBType -> jsonb);
#   - unbounded character types are ONE token ("text"): the archived SQL
#     chain used TEXT everywhere while the ORM declares unbounded String
#     (which create_all renders as VARCHAR with no length) — in Postgres
#     the two are storage- and semantics-equivalent, so this is not drift;
#   - bounded character types keep their length (varchar(3) != char(3) !=
#     text): a length bound appearing or disappearing IS drift;
#   - Numeric keeps precision/scale; DateTime keeps its timezone flag.
# Anything outside this mapping raises TypeError — a loud failure, never
# a silent pass.


def _type_where(column: Column[Any]) -> str:
    if column.table is not None:
        return f"{column.table.name}.{column.name}"
    return column.name


def _orm_type_token(column: Column[Any]) -> str:
    """Normalize an ORM column type to a comparison token."""
    from constat_api.orm import GUID, JSONBType

    t = column.type
    if isinstance(t, GUID):
        return "uuid"
    if isinstance(t, JSONBType):
        return "jsonb"
    if isinstance(t, Text):
        return "text"
    if isinstance(t, SA_CHAR):
        return f"char({t.length})"
    if isinstance(t, String):
        return "text" if t.length is None else f"varchar({t.length})"
    if isinstance(t, BigInteger):
        return "bigint"
    if isinstance(t, SmallInteger):
        return "smallint"
    if isinstance(t, Integer):
        return "integer"
    if isinstance(t, Boolean):
        return "boolean"
    # Float subclasses Numeric — check it first.
    if isinstance(t, Float):
        return "double precision"
    if isinstance(t, Numeric):
        return f"numeric({t.precision},{t.scale})"
    if isinstance(t, DateTime):
        return "timestamptz" if t.timezone else "timestamp"
    if isinstance(t, Date):
        return "date"
    raise TypeError(
        f"unmapped ORM type {t!r} on {_type_where(column)} — extend "
        "_orm_type_token with an explicit mapping; never let it pass silently"
    )


def _db_type_token(reflected: Any) -> str:
    """Normalize a PG-reflected column type (SQLAlchemy Inspector) to a token."""
    from sqlalchemy.dialects.postgresql import JSONB, UUID

    if isinstance(reflected, UUID):
        return "uuid"
    if isinstance(reflected, JSONB):
        return "jsonb"
    # Reflected dialect types subclass the generic ones (postgresql.VARCHAR
    # is a String, postgresql.TEXT a Text, ...), so generic isinstance
    # checks suffice — same order as _orm_type_token.
    if isinstance(reflected, Text):
        return "text"
    if isinstance(reflected, SA_CHAR):
        return f"char({reflected.length})"
    if isinstance(reflected, String):
        return "text" if reflected.length is None else f"varchar({reflected.length})"
    if isinstance(reflected, BigInteger):
        return "bigint"
    if isinstance(reflected, SmallInteger):
        return "smallint"
    if isinstance(reflected, Integer):
        return "integer"
    if isinstance(reflected, Boolean):
        return "boolean"
    if isinstance(reflected, Float):
        return "double precision"
    if isinstance(reflected, Numeric):
        return f"numeric({reflected.precision},{reflected.scale})"
    if isinstance(reflected, DateTime):
        return "timestamptz" if reflected.timezone else "timestamp"
    if isinstance(reflected, Date):
        return "date"
    raise TypeError(
        f"unmapped reflected Postgres type {reflected!r} — extend "
        "_db_type_token with an explicit mapping; never let it pass silently"
    )


# ---------------------------------------------------------------------------
# The shared schema differ
# ---------------------------------------------------------------------------


def diff_schema_against_metadata(dsn: str) -> list[str]:
    """Diff the live `public` schema against `Base.metadata`.

    Returns a list of human-readable drift descriptions; empty means the
    database schema and the ORM agree exactly. Rules:

    - every ORM table must exist; every DB table must be in the ORM or in
      EXTRA_TABLES_ALLOWLIST;
    - every ORM column must exist with a matching normalized type and
      nullability; every DB column must be in the ORM or in
      EXTRA_COLUMNS_ALLOWLIST (empty today);
    - defaults are compared loosely (presence only): a server_default the
      ORM declares must exist in the DB ("server default lost"), and a DB
      default the ORM knows nothing about — neither server_default nor a
      client-side default — is flagged ("database default unknown to the
      ORM"). Client-side defaults (uuid4, dict, ...) intentionally satisfy
      the check: the ORM path can always omit the column. `nextval(...)`
      defaults are skipped — they are the serial mechanism behind
      autoincrement PKs, not schema drift.

    Indexes, constraints, RLS policies, and triggers are out of scope here
    (RLS is pinned by tests/test_rls.py).
    """
    from constat_api.orm import Base

    engine = create_engine(dsn)
    try:
        inspector = inspect(engine)
        db_tables = set(inspector.get_table_names(schema="public"))
        meta_tables = Base.metadata.tables

        problems: list[str] = []

        for name in sorted(set(meta_tables) - db_tables):
            problems.append(
                f"missing table: {name} (declared in the ORM, absent from the database)"
            )
        for name in sorted(db_tables - set(meta_tables) - EXTRA_TABLES_ALLOWLIST):
            problems.append(
                f"unexpected table: {name} (in the database, not in the ORM, "
                "not in EXTRA_TABLES_ALLOWLIST)"
            )

        for name, table in sorted(meta_tables.items()):
            if name not in db_tables:
                continue  # already reported above
            db_columns = {c["name"]: c for c in inspector.get_columns(name, schema="public")}

            for column in table.columns:
                reflected = db_columns.get(column.name)
                if reflected is None:
                    problems.append(
                        f"missing column: {name}.{column.name} "
                        "(declared in the ORM, absent from the database)"
                    )
                    continue
                orm_token = _orm_type_token(column)
                db_token = _db_type_token(reflected["type"])
                if orm_token != db_token:
                    problems.append(
                        f"type mismatch: {name}.{column.name}: "
                        f"ORM {orm_token} vs database {db_token}"
                    )
                if bool(reflected["nullable"]) != bool(column.nullable):
                    problems.append(
                        f"nullability mismatch: {name}.{column.name}: "
                        f"ORM nullable={column.nullable} vs database nullable={reflected['nullable']}"
                    )
                db_default = reflected["default"]
                if column.server_default is not None and db_default is None:
                    problems.append(
                        f"server default lost: {name}.{column.name} "
                        f"(ORM server_default={column.server_default}, database has none)"
                    )
                if (
                    db_default is not None
                    and "nextval(" not in str(db_default)
                    and column.server_default is None
                    and column.default is None
                ):
                    problems.append(
                        f"database default unknown to the ORM: {name}.{column.name} "
                        f"DEFAULT {db_default}"
                    )

            allowed = EXTRA_COLUMNS_ALLOWLIST.get(name, frozenset())
            orm_column_names = {c.name for c in table.columns}
            for col_name in sorted(set(db_columns) - orm_column_names - allowed):
                problems.append(
                    f"unexpected column: {name}.{col_name} (in the database, not in "
                    "the ORM, not in EXTRA_COLUMNS_ALLOWLIST — if the migration chain "
                    "lost this column from the ORM's view, map it; do not allow-list)"
                )

        return problems
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Alembic helpers (the stamp half; _apply_alembic_schema covers upgrade)
# ---------------------------------------------------------------------------


def _baseline_revision_id() -> str:
    """The revision with no down_revision in db/alembic/versions/ (the squash anchor)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    bases = [s.revision for s in script.walk_revisions() if not s.down_revision]
    if len(bases) != 1:
        raise RuntimeError(
            f"expected exactly one baseline revision (no down_revision), found {bases}"
        )
    return bases[0]


def _alembic_stamp(dsn: str, revision: str) -> None:
    """`alembic stamp <revision>` against `dsn` (same env-var knob as upgrade)."""
    import os

    from alembic import command
    from alembic.config import Config

    previous = os.environ.get("CONSTAT_DATABASE_URL")
    os.environ["CONSTAT_DATABASE_URL"] = dsn  # db/alembic/env.py reads this knob
    try:
        command.stamp(Config(str(ALEMBIC_INI)), revision)
    finally:
        if previous is None:
            os.environ.pop("CONSTAT_DATABASE_URL", None)
        else:
            os.environ["CONSTAT_DATABASE_URL"] = previous


# ---------------------------------------------------------------------------
# Fixtures: two virgin schemas, one per proof
# ---------------------------------------------------------------------------


def _reset_public_schema() -> None:
    """Drop and recreate `public` — the convention tests.test_rls established."""
    psycopg = _psycopg()
    with psycopg.connect(
        DATABASE_URL, autocommit=True, cursor_factory=psycopg.ClientCursor
    ) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture(scope="module")
def pg_fresh_install() -> Iterator[str]:
    """Virgin schema + `alembic upgrade head` (proof A)."""
    if not DATABASE_URL:
        pytest.skip("CONSTAT_TEST_DATABASE_URL unset — migration-chain tests need a live database")
    _reset_public_schema()
    try:
        _apply_alembic_schema(DATABASE_URL)
    except Exception as exc:
        pytest.fail(
            "`alembic upgrade head` failed on a virgin schema — the chain cannot "
            "bootstrap a fresh install. The baseline revision must create (or the "
            "chain must otherwise reach) the full post-squash schema; a no-op "
            "baseline followed by revisions that assume existing tables breaks "
            "here. Root cause: "
            f"{type(exc).__name__}: {exc}"
        )
    yield DATABASE_URL
    _reset_public_schema()  # leave the schema clean for the other postgres tests


@pytest.fixture(scope="module")
def pg_presquash_chain() -> Iterator[str]:
    """Virgin schema + archived SQL dump + stamp baseline + upgrade head (proof B)."""
    if not DATABASE_URL:
        pytest.skip("CONSTAT_TEST_DATABASE_URL unset — migration-chain tests need a live database")
    psycopg = _psycopg()
    _reset_public_schema()
    with psycopg.connect(
        DATABASE_URL, autocommit=True, cursor_factory=psycopg.ClientCursor
    ) as conn:
        for path in sorted(ARCHIVED_MIGRATIONS_DIR.glob("*.sql")):
            try:
                conn.execute(path.read_text(encoding="utf-8"))
            except Exception as exc:
                pytest.fail(
                    f"archived migration {path.name} failed on a virgin schema — the "
                    f"pre-squash chain itself is broken: {type(exc).__name__}: {exc}"
                )
    _alembic_stamp(DATABASE_URL, _baseline_revision_id())
    try:
        _apply_alembic_schema(DATABASE_URL)
    except Exception as exc:
        pytest.fail(
            "`alembic upgrade head` failed after stamping the baseline onto the "
            "pre-squash schema — post-baseline revisions don't apply to the schema "
            f"the archived chain produces: {type(exc).__name__}: {exc}"
        )
    yield DATABASE_URL
    _reset_public_schema()


# ---------------------------------------------------------------------------
# The two proofs
# ---------------------------------------------------------------------------


@requires_postgres
@pytest.mark.postgres
class TestMigrationChain:
    """The migration chain, pinned to the ORM at both ends. Live Postgres only."""

    def test_fresh_install_matches_orm(self, pg_fresh_install: str) -> None:
        """`alembic upgrade head` on a virgin schema ≡ Base.metadata."""
        problems = diff_schema_against_metadata(pg_fresh_install)
        assert problems == [], "fresh-install schema drifted from the ORM:\n  " + "\n  ".join(
            problems
        )

    def test_presquash_dump_reaches_same_head(self, pg_presquash_chain: str) -> None:
        """pre-squash dump + stamp baseline + upgrade head ≡ Base.metadata.

        This is the seam proof: an existing pre-squash database reaches the
        same head as a fresh install, so no column can be lost in the
        squash without failing here.
        """
        problems = diff_schema_against_metadata(pg_presquash_chain)
        assert problems == [], (
            "pre-squash chain (archived SQL + stamp + upgrade) drifted from the "
            "ORM:\n  " + "\n  ".join(problems)
        )


# ---------------------------------------------------------------------------
# Unit: the type-token mapping itself (no database needed)
# ---------------------------------------------------------------------------


def test_every_orm_column_has_a_mapped_type_token() -> None:
    """Completeness guard: no ORM column may hit the loud-failure path."""
    from constat_api.orm import Base

    for table in Base.metadata.tables.values():
        for column in table.columns:
            token = _orm_type_token(column)
            assert isinstance(token, str) and token, f"{table.name}.{column.name}"


def test_unmapped_orm_type_fails_loudly() -> None:
    """An ORM type with no explicit mapping must raise, never pass silently."""
    from sqlalchemy import ARRAY

    with pytest.raises(TypeError, match="unmapped ORM type"):
        _orm_type_token(Column("arr", ARRAY(String)))


def test_db_type_tokens_match_orm_tokens_for_equivalent_types() -> None:
    """The two normalization directions must agree on the token language."""
    from sqlalchemy.dialects.postgresql import (
        BIGINT,
        BOOLEAN,
        CHAR,
        DATE,
        DOUBLE_PRECISION,
        INTEGER,
        JSONB,
        NUMERIC,
        TEXT,
        TIMESTAMP,
        UUID,
        VARCHAR,
    )

    cases: list[tuple[Any, str]] = [
        (UUID(), "uuid"),
        (JSONB(), "jsonb"),
        (TEXT(), "text"),
        (VARCHAR(), "text"),  # unbounded varchar ≡ text in Postgres
        (VARCHAR(3), "varchar(3)"),
        (CHAR(3), "char(3)"),
        (BIGINT(), "bigint"),
        (INTEGER(), "integer"),
        (BOOLEAN(), "boolean"),
        (DOUBLE_PRECISION(), "double precision"),
        (NUMERIC(18, 6), "numeric(18,6)"),
        (TIMESTAMP(timezone=True), "timestamptz"),
        (TIMESTAMP(), "timestamp"),
        (DATE(), "date"),
    ]
    for reflected, expected in cases:
        assert _db_type_token(reflected) == expected, repr(reflected)


def test_unmapped_db_type_fails_loudly() -> None:
    from sqlalchemy.dialects.postgresql import ARRAY

    with pytest.raises(TypeError, match="unmapped reflected Postgres type"):
        _db_type_token(ARRAY(String))


def test_baseline_revision_is_discoverable() -> None:
    """The stamp target must resolve to the actual no-down_revision anchor."""
    assert _baseline_revision_id() == "8574f09d5d61"
