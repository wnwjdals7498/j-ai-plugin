"""Local implementation of the operation-level storage port."""
from .db import Database


class LocalStore:
    def __init__(self, db, executor=None):
        self.db = db if isinstance(db, Database) else Database(db)
        self.executor = executor

    def execute(self, request):
        if self.executor:
            return self.executor(self.db, request)
        from .service import execute
        return execute(self.db, request)

    def get_request_result(self, request_id, actor=None, session_id=None, *, expected_request=None):
        if expected_request is not None and expected_request.get("protocol_version") == 1:
            from .service import normalize_lookup_request
            expected_request = normalize_lookup_request(expected_request)
        return self.db.get_request_result(request_id, actor=actor, session_id=session_id,
                                          expected_request=expected_request)

    def check_compatibility(self):
        from . import __version__
        from .db import SCHEMA_VERSION
        from .planning.graph import SCHEMA_VERSION as GRAPH_SCHEMA_VERSION
        return {"compatible": True, "core_version": __version__, "db_schema": SCHEMA_VERSION,
                "graph_schema": GRAPH_SCHEMA_VERSION, "protocol_versions": [1],
                "storage": "local-sqlite"}
