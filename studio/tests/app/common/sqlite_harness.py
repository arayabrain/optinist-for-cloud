"""An in-memory SQLite session for tests that need a real database.

Why not a mock: with `Mock(spec=Session)` every column reads back whatever the
test assigned, so "only these columns moved", "the row was replaced rather than
added to" and "nothing was written" are all unprovable.

Two operational facts, both of which outlive any single caller:

- Importing this module mutates SQLAlchemy's dialect-compiler registry for the
  whole process, so every SQLite-backed test afterwards emits INTEGER for BIGINT
  and TINYINT columns, and renders MySQL's ``ON DUPLICATE KEY UPDATE`` as
  SQLite's ``ON CONFLICT ... DO UPDATE``. There is no way to undo a ``@compiles``
  registration.
- What the harness cannot show, and which therefore stays a production check:
  ``SELECT ... FOR UPDATE`` compiles away, ``SQLEnum`` emits no CHECK constraint,
  and foreign keys are unenforced. Column-level behaviour does hold, including
  the ORM-level ``onupdate`` on ``updated_at``.
"""

from contextlib import contextmanager

from sqlalchemy import BIGINT as GENERIC_BIGINT
from sqlalchemy import BigInteger
from sqlalchemy.dialects.mysql import BIGINT as MYSQL_BIGINT
from sqlalchemy.dialects.mysql import TINYINT as MYSQL_TINYINT
from sqlalchemy.dialects.mysql.dml import OnDuplicateClause
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql import coercions, roles
from sqlmodel import Session, SQLModel, create_engine


# SQLite only autoincrements an INTEGER PRIMARY KEY, not BIGINT, and has no
# TINYINT at all. Registering this is permanent for the process.
@compiles(BigInteger, "sqlite")
@compiles(GENERIC_BIGINT, "sqlite")
@compiles(MYSQL_BIGINT, "sqlite")
@compiles(MYSQL_TINYINT, "sqlite")
def _integer_on_sqlite(type_, compiler, **kw):
    return "INTEGER"


# MySQL infers which unique key collided; SQLite needs it named.
@compiles(OnDuplicateClause, "sqlite")
def _on_conflict_on_sqlite(clause, compiler, **kw):
    table = compiler.statement.table
    target = [col.name for col in table.columns if col.unique] or list(
        table.primary_key.columns.keys()
    )
    assignments = ", ".join(
        "{} = {}".format(
            name,
            compiler.process(
                coercions.expect(roles.ExpressionElementRole, value), **kw
            ),
        )
        for name, value in clause.update.items()
    )
    return f"ON CONFLICT ({', '.join(target)}) DO UPDATE SET {assignments}"


@contextmanager
def sqlite_session(tables):
    """Yield a session over an in-memory database holding exactly ``tables``.

    ``tables`` is a list of ``Model.__table__``. Name only what the code under
    test touches: the full metadata pulls in tables whose DDL SQLite cannot
    compile, and a shorter list makes the test's blast radius readable.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    stripped = []
    for table in tables:
        for col in table.columns:
            arg = getattr(col.server_default, "arg", None)
            if arg is not None and "ON UPDATE" in str(arg):
                stripped.append((col, col.server_default))
                col.server_default = None
    try:
        SQLModel.metadata.create_all(engine, tables=tables)
        with Session(engine) as session:
            yield session
    finally:
        for col, default in stripped:
            col.server_default = default
