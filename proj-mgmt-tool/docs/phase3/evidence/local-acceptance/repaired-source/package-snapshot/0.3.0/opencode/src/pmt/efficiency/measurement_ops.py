"""Protocol adapters for immutable measurement manifests."""
from ..errors import PmtError
from ..phase2_common import event, validate_scope
from ..util import new_id
from .measurement import MeasurementError, capture_baseline, compare
from .storage import Phase3Storage

WRITE_OPERATIONS = {"capture_measurement", "compare_measurements"}
READ_OPERATIONS = FILE_OPERATIONS = set()


def handle(db, conn, req):
    scope = req.get("scope_id")
    validate_scope(db, conn, scope)
    p = req["payload"]
    store = Phase3Storage(db)
    try:
        if req["operation"] == "capture_measurement":
            if set(p) - {"case", "condition"}:
                raise PmtError("unknown_fields", "Unsupported measurement fields")
            result = capture_baseline(p.get("case"), p.get("condition"))
            kind = "measurement_baseline"
        else:
            if set(p) - {"baseline_id", "measured"}:
                raise PmtError("unknown_fields", "Unsupported comparison fields")
            baseline = store.get_object("measurement_baseline", p.get("baseline_id"), scope,
                                        req["actor"], req["session_id"], conn=conn)
            if baseline is None:
                raise PmtError("measurement_not_found", "Baseline is unavailable in this scope", 3)
            result = compare(baseline["body"], p.get("measured"))
            kind = "measurement_comparison"
    except MeasurementError as exc:
        raise PmtError("measurement_invalid", "Measurement conditions or observations are invalid", 2) from exc
    result_id = new_id()
    receipt = store.put_object(kind, result_id, scope, req["actor"], req["session_id"],
                               result.get("manifest_fingerprint") or result["comparison_fingerprint"],
                               0, result, conn=conn)
    event(conn, req, "efficiency.measurement_saved", scope_id=scope,
          payload={"measurement_id": result_id, "kind": kind, "revision": receipt["revision"]})
    return {"measurement_id": result_id, "manifest": result}
