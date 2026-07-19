"""Multi-tenant session context.

The tenant id is resolved from the authenticated principal (roadmap 3.1:
the API key's configured tenant) and set on every session by `get_db`
via `bind_tenant`. The default tenant remains the fallback for
anonymous/open requests.

How it works:
- The application sets `session.info["tenant_id"]` on each session.
- An `after_begin` SQLAlchemy event fires on every new transaction
  (including after a commit) and runs `SELECT set_config(...)` to
  install the tenant id into the `app.current_tenant_id` GUC.
- The migration `0007_rls_policies.sql` creates Postgres RLS policies
  that compare `tenant_id` to `current_setting('app.current_tenant_id', true)::uuid`.
- When the GUC is not set, `current_setting(..., true)` returns NULL,
  so `tenant_id = NULL` is always false, and the policy hides every row.
  This is the safe default.

The handler is a no-op on non-Postgres dialects (sqlite tests). It only
sets the GUC on Postgres, where RLS exists.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# GUC name used by the RLS policies in 0007_rls_policies.sql.
# Keep this in sync with the migration — drift is a silent security bug.
TENANT_GUC = "app.current_tenant_id"


def bind_tenant(session: Session, tenant_id: UUID | str | None) -> None:
    """Stash the tenant id on the session.

    The `after_begin` event picks it up on the next transaction and
    installs it into the Postgres GUC. No SQL runs at bind time.

    The tenant id always comes from the authenticated identity (the API
    key's configured tenant), never from the request.
    """
    if tenant_id is None:
        session.info.pop("tenant_id", None)
    else:
        session.info["tenant_id"] = UUID(str(tenant_id))


def current_tenant(session: Session) -> UUID | None:
    """Return the tenant id bound to this session, or None if unbound."""
    return session.info.get("tenant_id")


@event.listens_for(Session, "after_begin")
def _apply_tenant_guc(session: Session, transaction: Any, connection: Any) -> None:
    """Install the tenant GUC at the start of every transaction.

    Fires on:
    - session creation (first implicit begin)
    - session.commit() (auto-restart)
    - session.rollback() (auto-restart)
    - explicit session.begin()

    The runner relies on this: it calls `session.commit()` mid-execution,
    and the GUC is re-applied on the next transaction so RLS keeps filtering.
    """
    if connection.dialect.name != "postgresql":
        return  # RLS is Postgres-only. Sqlite tests are a no-op.

    tenant_id = session.info.get("tenant_id")
    if tenant_id is None:
        # The migration's policy uses `current_setting(..., true)` which
        # returns NULL for a missing GUC, so queries are correctly denied.
        # We do not need to clear anything here; the previous transaction's
        # SET LOCAL was already rolled back by the commit.
        return

    # set_config(name, value, is_local) — is_local=true => SET LOCAL semantics.
    # Parameterized: safe against SQL injection (the tenant id comes from
    # auth, but defense in depth is cheap).
    connection.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": TENANT_GUC, "value": str(tenant_id)},
    )


@event.listens_for(Session, "after_commit")
def _noop_on_commit(session: Session) -> None:
    """Placeholder for symmetry. The next transaction's after_begin re-applies."""
    # Intentionally empty. We document this because readers may wonder why
    # we don't clear session.info here. The answer: bind_tenant is sticky
    # for the whole session; only the GUC is transaction-scoped.
    pass
