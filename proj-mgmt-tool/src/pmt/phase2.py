"""Phase-two operation registry; adapters share the existing protocol boundary."""
from importlib import import_module

# This is the public protocol, not a list supplied by untrusted callers.
MODULES = {
    "pmt.planning": {
        "validate_plan_graph", "read_plan", "save_plan_draft", "publish_project_docs"},
    "pmt.steps": {
        "save_step_directive", "read_step", "read_step_directive", "review_step",
        "set_task_metadata", "invalidate_plan_branch", "cancel_step"},
    "pmt.routing": {
        "save_routing_policy", "read_routing_policy", "register_capabilities", "inspect_capabilities",
        "select_execution_route"},
    "pmt.execution": {
        "enqueue_execution", "prepare_execution", "attach_execution_handle", "observe_execution",
        "submit_execution_result", "request_execution_cancel", "reconcile_execution", "review_execution",
        "retry_execution", "extend_execution_scopes", "read_execution", "list_execution_queue"},
    "pmt.reconciliation": {"sync_project_baseline", "read_project_baseline"},
    "pmt.operations": {"read_progress", "observe_progress", "plan_retention", "execute_retention",
                       "diagnose_execution", "configure_diagnostics"},
    "pmt.runners": {"dispatch_execution", "poll_execution", "cancel_runner"},
}
OPERATIONS = set().union(*MODULES.values())


def execute(db, req):
    module_name = next(name for name, operations in MODULES.items() if req["operation"] in operations)
    module = import_module(module_name)
    if req["operation"] in getattr(module, "FILE_OPERATIONS", set()):
        return module.execute_file(db, req)
    if req["operation"] in getattr(module, "WRITE_OPERATIONS", set()):
        return db.run_request(req, lambda conn, request: module.handle(db, conn, request))
    if req["operation"] in getattr(module, "READ_OPERATIONS", set()):
        from .service import response
        with db.connect() as conn:
            result = module.handle(db, conn, req)
        return response(req["request_id"], result=result), 0
    from .errors import PmtError
    raise PmtError("operation_unavailable", "Operation is not available in this runtime", 2)
