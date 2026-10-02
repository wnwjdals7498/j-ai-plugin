"""Local-only adapter for the existing Phase-2 runner boundary.

This adapter never manufactures a process/handle. Dispatch, cancel and terminal
collection remain the existing runner operations; nonterminal observation is a
read-only snapshot and never emits a poll event or changes run state.
"""
from __future__ import annotations

import uuid

from ..errors import PmtError


class LocalExecutionRuntime:
    def __init__(self, local_store, *, clock=None):
        self.local_store = local_store
        self.clock = clock
        compatibility = local_store.check_compatibility()
        if not compatibility.get("compatible") or compatibility.get("storage") != "local-sqlite":
            raise PmtError("local_runtime_unsupported", "Local runner requires the verified local SQLite runtime", 3)

    def dispatch(self, request):
        operation = self._request(request, "dispatch_execution")
        return self.local_store.execute(operation)

    def observe(self, request):
        from ..runners.service import observe_local_runtime
        snapshot = observe_local_runtime(self.local_store.db, request)
        return snapshot

    def collect_terminal(self, request, observation):
        """Allow the existing runner service to persist a real local receipt once."""
        if not isinstance(observation, dict) or observation.get("status") != "terminal":
            raise PmtError("runner_not_terminal", "Only an observed terminal receipt can be collected", 3)
        run_id = request.get("payload", {}).get("run_id")
        receipt_hash = observation.get("receipt_sha256")
        if not isinstance(receipt_hash, str) or len(receipt_hash) != 64:
            raise PmtError("runner_receipt_unverified", "Terminal receipt hash is unavailable", 3)
        poll_id = str(uuid.uuid5(uuid.UUID(run_id), "pmt-f8-terminal-poll:" + receipt_hash))
        operation = self._request(request, "poll_execution", request_id=poll_id)
        return self.local_store.execute(operation)

    def cancel(self, request):
        operation = self._request(request, "cancel_runner")
        return self.local_store.execute(operation)

    def capabilities(self, run):
        route = run.get("route") if isinstance(run, dict) else None
        if not isinstance(route, dict):
            return {"state": "unknown", "reason": "runner_route_unavailable"}
        mode, agent = route.get("mode"), route.get("agent")
        if mode in {"native", "subagent"}:
            if route.get("actual_support") != "verified_supported":
                return {"state": "unsupported", "reason": "native_capability_unverified"}
            return {"state": "supported", "kind": "main_native_call",
                    "capability_ref": route.get("capability_ref")}
        if mode == "cli" and agent in {"codex", "claude"} and route.get("auth_state") == "authenticated":
            return {"state": "supported", "kind": "local_cli", "agent": agent,
                    "capability_ref": route.get("capability_ref")}
        return {"state": "unsupported", "reason": "local_cli_capability_unverified"}

    @staticmethod
    def _request(request, operation, *, request_id=None):
        value = {key: item for key, item in request.items()}
        value["operation"] = operation
        if request_id:
            value["request_id"] = request_id
        # Batch membership and prompt material are rederived by the runner from the
        # owner-bound F9 binding; never forward caller-constructed group claims.
        payload = {"run_id": request.get("payload", {}).get("run_id")}
        if operation == "dispatch_execution" and request.get("payload", {}).get("context_ref") is not None:
            payload["context_ref"] = request["payload"]["context_ref"]
        value["payload"] = payload
        return value
