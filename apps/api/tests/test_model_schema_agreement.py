"""The ORM models and the SQL migrations must describe the same database.

db/001_init.sql is the source of truth and nothing calls `create_all`, so the
models could drift from it indefinitely without any test noticing -- right up
until a query selects a column that does not exist, in production, at runtime.

This compares what SQLAlchemy believes against what Postgres actually has.
"""

from __future__ import annotations

import pytest
from commonground_api.models import Base
from sqlalchemy import inspect

pytestmark = pytest.mark.db


def test_every_model_maps_to_a_real_table(engine) -> None:
    actual = set(inspect(engine).get_table_names())
    declared = set(Base.metadata.tables)

    missing = declared - actual
    assert not missing, (
        f"models declare tables that no migration creates: {sorted(missing)}. "
        "Add a migration in db/, do not rely on create_all."
    )


def test_every_model_column_exists_in_the_database(engine) -> None:
    inspector = inspect(engine)
    problems: dict[str, list[str]] = {}

    for table_name, table in Base.metadata.tables.items():
        if table_name not in inspector.get_table_names():
            continue
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        declared = {column.name for column in table.columns}
        missing = declared - actual
        if missing:
            problems[table_name] = sorted(missing)

    assert not problems, f"model columns with no database column: {problems}"


def test_no_table_has_columns_the_models_do_not_know_about(engine) -> None:
    """The other direction, which is the one that produces silent data loss.

    A column added by a migration but not by the model is invisible to the
    application: inserts leave it at its default and nobody notices until the
    value is needed. schema_migrations is the migrator's own bookkeeping and is
    deliberately not modelled.
    """
    inspector = inspect(engine)
    problems: dict[str, list[str]] = {}

    for table_name in inspector.get_table_names():
        if table_name == "schema_migrations":
            continue
        table = Base.metadata.tables.get(table_name)
        if table is None:
            problems[table_name] = ["<no model at all>"]
            continue
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        declared = {column.name for column in table.columns}
        unmodelled = actual - declared
        if unmodelled:
            problems[table_name] = sorted(unmodelled)

    assert not problems, f"database columns the models do not know about: {problems}"


def test_primary_keys_agree(engine) -> None:
    inspector = inspect(engine)
    problems: dict[str, tuple[list[str], list[str]]] = {}

    for table_name, table in Base.metadata.tables.items():
        if table_name not in inspector.get_table_names():
            continue
        actual = sorted(inspector.get_pk_constraint(table_name)["constrained_columns"])
        declared = sorted(column.name for column in table.primary_key.columns)
        if actual != declared:
            problems[table_name] = (declared, actual)

    assert not problems, f"primary keys disagree (model, database): {problems}"
