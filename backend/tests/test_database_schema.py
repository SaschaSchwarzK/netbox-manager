from sqlalchemy import create_engine, inspect, text

from app.database import upgrade_existing_schema


def test_upgrade_existing_schema_adds_polymorphic_patch_column_idempotently():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE migration_job_patches ("
            "id VARCHAR(36) PRIMARY KEY, patch_fields_json TEXT NOT NULL DEFAULT '{}'"
            ")"
        ))
        connection.execute(text(
            "INSERT INTO migration_job_patches (id, patch_fields_json) VALUES ('existing', '{}')"
        ))

    upgrade_existing_schema(engine)
    upgrade_existing_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("migration_job_patches")}
    assert "polymorphic_patch_fields_json" in columns
    with engine.connect() as connection:
        value = connection.execute(text(
            "SELECT polymorphic_patch_fields_json FROM migration_job_patches WHERE id='existing'"
        )).scalar_one()
    assert value == "{}"
