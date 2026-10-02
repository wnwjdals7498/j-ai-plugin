"""Explicit phase-three CLI registry; this is not the Host allowlist."""
from importlib import import_module
from contextlib import closing

from .errors import PmtError

CONTRACT_VERSION = "phase3-1"
MODULES = {
    "pmt.efficiency.graph": {
        "capture_source_pin", "preview_graph_change", "apply_graph_change", "recover_graph_change",
        "query_graph", "rebuild_graph_index", "calculate_graph_impact", "register_segment_manifest"},
    "pmt.efficiency.documents": {
        "prepare_document_segments", "publish_document_segments", "recover_document_segments"},
    "pmt.efficiency.context": {
        "build_task_context", "read_context_detail", "resolve_context_alias", "resume_task_context",
        "read_task_context"},
    "pmt.efficiency.reuse": {
        "resolve_reuse", "record_reuse_result", "invalidate_reuse", "read_reuse_decision"},
    "pmt.efficiency.results": {
        "compact_tool_result", "read_tool_result_detail"},
    "pmt.efficiency.control": {
        "advance_execution_control", "read_execution_control", "acknowledge_execution_action"},
    "pmt.efficiency.batch": {
        "prepare_step_batch", "bind_step_batch", "collect_step_batch"},
    "pmt.efficiency.measurement_ops": {
        "capture_measurement", "compare_measurements"},
}
OPERATIONS = set().union(*MODULES.values())


def execute(db, req):
    name = next((name for name, ops in MODULES.items() if req["operation"] in ops), None)
    if name is None:
        raise PmtError("operation_unsupported", "Unsupported phase-three operation")
    module = import_module(name)
    # File/process effects own their journal and never run inside a DB transaction.
    if req["operation"] in getattr(module, "FILE_OPERATIONS", set()):
        return module.execute_file(db, req)
    if req["operation"] in getattr(module, "WRITE_OPERATIONS", set()):
        return db.run_request(req, lambda conn, request: module.handle(db, conn, request))
    if req["operation"] in getattr(module, "READ_OPERATIONS", set()):
        from .service import response
        with closing(db.connect()) as conn:
            result = module.handle(db, conn, req)
        return response(req["request_id"], result=result), 0
    raise PmtError("operation_unavailable", "Phase-three operation is unavailable in this runtime")
