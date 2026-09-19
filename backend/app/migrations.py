"""
There's no Alembic wiring in this app (see README) — instead, on startup we
diff each mapped table's columns against what's actually in the SQLite file
and ALTER TABLE ADD COLUMN anything missing. This only ever adds columns,
never removes/renames/retypes them, so it's safe to run unconditionally on
every startup and requires no version tracking of its own.
"""
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def _sql_default_literal(column) -> str | None:
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def run_lightweight_migrations(engine: Engine, base) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # brand new table — create_all() already created it with every column
            existing_columns = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                col_type = column.type.compile(engine.dialect)
                default_literal = _sql_default_literal(column)
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
                if default_literal is not None:
                    ddl += f" DEFAULT {default_literal}"
                conn.execute(text(ddl))
