"""Explicit storage-only Host extension registry.

This registry is consumed by the Host adapter, not by the local Phase 3 CLI.
Local file publication, Git edits, runner dispatch, and model routing remain
client operations.
"""

SOURCE_OPERATIONS = frozenset({
    "authorize_workspace", "publish_source_snapshot", "read_source_snapshot", "read_source_metadata",
})
VERIFICATION_OPERATIONS = frozenset({
    "publish_verification_snapshot", "record_verification", "lookup_verification",
})
GRAPH_OPERATIONS = frozenset({
    "capture_source_pin", "rebuild_graph_index", "query_graph", "calculate_graph_impact",
    "register_segment_manifest",
})
CONTEXT_OPERATIONS = frozenset({
    "build_task_context", "resume_task_context", "read_task_context",
    "read_context_detail", "resolve_context_alias",
})
REUSE_OPERATIONS = frozenset({"read_reuse_decision"})
REUSE_FILE_OPERATIONS = frozenset({"resolve_reuse", "record_reuse_result", "invalidate_reuse"})
RESULT_OPERATIONS = frozenset({"compact_tool_result", "read_tool_result_detail"})
STEP_RESOURCE_OPERATIONS = frozenset({"save_step_directive", "read_step_directive"})
BATCH_OPERATIONS = frozenset({"prepare_step_batch", "bind_step_batch", "collect_step_batch",
                              "read_step_batch"})
LOCAL_FILE_EFFECT_OPERATIONS = frozenset({"begin_local_file_effect", "complete_local_file_effect",
                                         "read_local_file_effect"})
PLAN_METADATA_OPERATIONS = frozenset({"publish_client_plan", "read_client_plan"})

HOST_DATA_OPERATIONS = (SOURCE_OPERATIONS | VERIFICATION_OPERATIONS | GRAPH_OPERATIONS
                        | CONTEXT_OPERATIONS | REUSE_OPERATIONS | REUSE_FILE_OPERATIONS | RESULT_OPERATIONS
                        | STEP_RESOURCE_OPERATIONS | BATCH_OPERATIONS | LOCAL_FILE_EFFECT_OPERATIONS
                        | PLAN_METADATA_OPERATIONS)

READ_OPERATIONS = frozenset({
    "authorize_workspace", "read_source_snapshot", "read_source_metadata", "capture_source_pin", "query_graph", "calculate_graph_impact",
    "read_task_context", "read_context_detail", "resolve_context_alias", "read_reuse_decision",
    "read_tool_result_detail", "read_step_directive", "lookup_verification",
    "read_step_batch",
    "read_local_file_effect",
    "read_client_plan",
})
FILE_OPERATIONS = frozenset({
    "publish_source_snapshot", "publish_verification_snapshot", "rebuild_graph_index",
    "register_segment_manifest",
    "build_task_context", "resume_task_context", "compact_tool_result", "save_step_directive",
    "resolve_reuse", "record_reuse_result", "invalidate_reuse",
    "prepare_step_batch", "bind_step_batch", "collect_step_batch",
    "begin_local_file_effect", "complete_local_file_effect",
    "publish_client_plan",
})
WRITE_OPERATIONS = HOST_DATA_OPERATIONS - READ_OPERATIONS - FILE_OPERATIONS

LOCAL_ONLY_OPERATIONS = frozenset({
    "preview_graph_change",
    "apply_graph_change", "recover_graph_change", "prepare_document_segments",
    "publish_document_segments", "recover_document_segments", "advance_execution_control",
    "acknowledge_execution_action", "dispatch_execution", "poll_execution", "cancel_runner",
})
