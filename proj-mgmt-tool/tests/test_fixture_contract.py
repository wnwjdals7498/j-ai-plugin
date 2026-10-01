"""Local fixture checks; these do not claim that a PMT product operation works."""

def test_fixture_request_has_protocol_v1_and_stable_session(request_factory):
    a = request_factory("read_context")
    b = request_factory("read_context")
    assert a["protocol_version"] == b["protocol_version"] == 1
    assert a["actor"] == b["actor"] == "main"
    assert a["session_id"] == b["session_id"]
    assert a["request_id"] != b["request_id"]


def test_fixture_roots_are_separate_and_user_scoped(roots):
    data, config = roots
    assert data != config
    assert data.exists() and config.exists()
