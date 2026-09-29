"""The legacy source: a read-only PostgreSQL session (probed on a table in the test database) and fixture tables."""

import pytest
from django.db import connection

from migrations_tools.services.source import PostgresSource, SourceError, TablesSource, checksum, open_source, redact_url

PROBE = "legacy_probe_rows"


@pytest.fixture
def probe_url(transactional_db):
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE TABLE {PROBE} (id integer PRIMARY KEY, name text, meta jsonb)")
        cursor.execute(f"INSERT INTO {PROBE} VALUES (2, 'b', '{{\"k\": 1}}'), (1, 'a', NULL)")
        cursor.execute("CREATE TABLE legacy_probe_noid (b text, a text)")
        cursor.execute("INSERT INTO legacy_probe_noid VALUES ('y', '2'), ('x', '1')")
    settings = connection.settings_dict
    yield f"postgresql://{settings['USER']}:{settings['PASSWORD']}@{settings['HOST'] or 'localhost'}:{settings['PORT'] or 5432}/{settings['NAME']}"
    with connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS {PROBE}, legacy_probe_noid")


def test_postgres_source_reads_plain_rows_in_id_order_and_never_writes(probe_url):
    import psycopg

    with open_source(probe_url) as source:
        assert source.has_table(PROBE) and not source.has_table("nope") and PROBE in source.table_names()
        assert source.rows(PROBE) == [{"id": 1, "name": "a", "meta": None}, {"id": 2, "name": "b", "meta": {"k": 1}}]
        assert source.rows("legacy_probe_noid") == [{"b": "x", "a": "1"}, {"b": "y", "a": "2"}]
        assert source.count(PROBE) == 2 and source.count("nope") == 0 and source.rows("nope") == []
        assert "***" in source.label and "postgres:" not in source.label.split("@")[0].split("//")[1][len("postgres:") :]
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with source.conn.cursor() as cursor:
                cursor.execute(f"DELETE FROM {PROBE}")


def test_fixture_source_and_helpers(tmp_path):
    path = tmp_path / "t.json"
    path.write_text('{"tables": {"a": [{"id": 1}]}}')
    source = open_source(fixture=str(path))
    assert isinstance(source, TablesSource) and source.rows("a") == [{"id": 1}] and source.count("b") == 0 and source.has_table("a")
    assert checksum([{"b": 1, "a": 2}]) == checksum([{"a": 2, "b": 1}])
    assert redact_url("postgresql://u:secret@h/db") == "postgresql://u:***@h/db" and redact_url("postgresql://h/db") == "postgresql://h/db"


def test_connection_errors_are_source_errors():
    with pytest.raises(SourceError, match="cannot connect"):
        PostgresSource("postgresql://nobody:wrong@127.0.0.1:1/none")
    with pytest.raises(SourceError, match="postgresql://"):
        open_source("sqlite:///x")
