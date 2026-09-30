import io

import pytest

from geovectors_encoder.services.vector_storage_service import VectorStorageService


class DummyPsycopgCursor:
    def __init__(self):
        self.queries = []

    def copy_expert(self, sql, fileobj):
        self.queries.append(sql)


class DummyCursor:
    def __init__(self):
        self.queries = []
        self._inner = DummyPsycopgCursor()

    @property
    def cursor(self):
        # Mimic Django's DatabaseWrapper cursor wrapper exposing underlying psycopg cursor
        return self._inner

    @property
    def connection(self):
        return self

    def execute(self, sql, params=None):
        self.queries.append(sql)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class DummyConnections:
    def __init__(self):
        self._cursor = DummyCursor()

    def __getitem__(self, alias):
        assert alias == "vectors"
        return self

    def cursor(self):
        return self._cursor


@pytest.mark.unit
def test_vector_storage_service_default_batch_size():
    storage = VectorStorageService()
    assert storage.batch_size == 50000


@pytest.mark.unit
def test_vector_storage_service_uses_unlogged_temp_table(monkeypatch):
    dummy_conns = DummyConnections()

    import geovectors_encoder.services.vector_storage_service as vs_module
    monkeypatch.setattr(vs_module, "connections", dummy_conns)

    storage = VectorStorageService(batch_size=2, model_type="tags", version="test_ver")

    # Prepare minimal buffer with two entities
    storage.buffer = [
        {
            "osm_type": "node",
            "osm_id": 1,
            "tags": [("name", "a")],
            "geom": None,
            "gv_tags_embedding": [0.1, 0.2],
        },
        {
            "osm_type": "node",
            "osm_id": 2,
            "tags": [("name", "b")],
            "geom": None,
            "gv_tags_embedding": [0.3, 0.4],
        },
    ]

    storage._bulk_upsert()

    cursor = dummy_conns._cursor
    create_sql = next(q for q in cursor.queries if "CREATE" in q)
    assert "CREATE UNLOGGED TABLE" in create_sql
