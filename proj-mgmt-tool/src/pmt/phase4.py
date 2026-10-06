"""Explicit local continuity registry; storage-only Host exposure is separate."""
from contextlib import closing
from importlib import import_module

from .continuity.contracts import authorize, bounded_result
from .errors import PmtError

CONTRACT_VERSION = "phase4-1"
MODULES = {
    "pmt.continuity.current": {"read_current_facts", "capture_work_basis", "validate_basis",
                               "create_checkpoint", "read_checkpoint", "link_session"},
    "pmt.continuity.context": {"compose_resume_overview", "compose_task_resume",
                               "read_resume_detail", "propose_next_action"},
    "pmt.continuity.changes": {"collect_changes", "build_implementation_links", "read_change_slice",
                               "register_observed_change"},
    "pmt.continuity.alignment": {"assess_alignment", "propose_semantic_resolution", "apply_alignment",
                                 "read_applicability"},
    "pmt.continuity.retention": {"prune_continuity"},
}
OPERATIONS = set().union(*MODULES.values())


def execute(db, req):
    name = next((name for name, ops in MODULES.items() if req["operation"] in ops), None)
    if name is None:
        raise PmtError("operation_unsupported", "Unsupported continuity operation")
    module = import_module(name)
    if req["operation"] in getattr(module, "FILE_OPERATIONS", set()):
        with closing(db.connect()) as conn:
            authorize(db, conn, req)
        result = module.execute_file(db, req)
        if isinstance(result, dict):
            from .service import response
            if result.get("state") in {"incomplete", "partial", "reconciliation_required"}:
                result.setdefault("complete", False)
            return response(req["request_id"], result=bounded_result(req, result)), 0
        if not isinstance(result, tuple) or len(result) != 2:
            raise PmtError("continuity_contract_invalid", "File service returned an invalid operation outcome", 5)
        return result
    if req["operation"] in getattr(module, "WRITE_OPERATIONS", set()):
        return db.run_request(req, lambda conn, request: module.handle(db, conn, request),
                              authorize=lambda conn, request: authorize(db, conn, request))
    if req["operation"] in getattr(module, "READ_OPERATIONS", set()):
        from .service import response
        with closing(db.connect()) as conn:
            conn.execute("BEGIN")
            authorize(db, conn, req)
            result = module.handle(db, conn, req)
        return response(req["request_id"], result=result), 0
    raise PmtError("operation_unavailable", "Continuity handler is unavailable")
