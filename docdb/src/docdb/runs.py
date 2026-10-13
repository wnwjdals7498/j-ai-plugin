"""실행 결과 적재 및 규칙 R4를 처리하는 모듈."""
from typing import Any

from docdb import clock, db, history, ids, policy, relations, textnorm
from docdb.actors import Actor
from docdb.db import Connection
from docdb.errors import DocdbError

_REQUIRED_KEYS = ("run_type", "target_ref", "result", "runner", "trust_source")
_ALLOWED_RESULTS = frozenset({"PASS", "FAIL", "NOT_RUN", "ERROR", "UNKNOWN"})
_ALLOWED_SEVERITIES = frozenset({"info", "warning", "error", "critical"})
_ALLOWED_TARGET_TYPES = frozenset(
    {"document", "item", "artifact", "work", "field", "project"}
)
_ALLOWED_DISPOSITIONS = frozenset(
    {"open", "fixed", "accepted_exception", "false_positive", "deferred"}
)
_ALLOWED_TRUST_SOURCES = frozenset({"ci", "runner", "manual", "ai_reported"})


def _execute(con: Connection, sql: str, args: tuple = ()) -> Any:
    """DB 오류를 계약 오류로 바꾸어 SQL을 실행한다."""
    try:
        return con.execute(sql, args)
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc


def ingest(con: Connection, actor: Actor, payload: dict) -> str:
    """실행 결과를 적재하고 연결된 증거물, 이력, R4 관계를 생성한 뒤 run_id를 반환한다."""
    if not isinstance(payload, dict):
        raise DocdbError("BAD_ARGS", "payload는 dict여야 함")

    for key in _REQUIRED_KEYS:
        val = payload.get(key)
        if val is None or val == "":
            raise DocdbError("BAD_ARGS", f"필수 키 누락: {key}")

    result = payload["result"]
    if result not in _ALLOWED_RESULTS:
        raise DocdbError("BAD_ARGS", f"허용되지 않은 result: {result}")

    work_id = payload.get("work_id")
    if work_id is not None:
        ids.validate("work", work_id)
        if _execute(con, "SELECT 1 FROM work WHERE work_id=?", (work_id,)).fetchone() is None:
            raise DocdbError("NOT_FOUND", f"작업을 찾을 수 없음: {work_id}")

    env_item_id = payload.get("env_item_id")
    if env_item_id is not None:
        if _execute(con, "SELECT 1 FROM item WHERE item_id=?", (env_item_id,)).fetchone() is None:
            raise DocdbError("NOT_FOUND", f"환경 항목을 찾을 수 없음: {env_item_id}")

    covers = payload.get("covers")
    if covers is not None:
        if not isinstance(covers, list):
            raise DocdbError("BAD_ARGS", "covers는 리스트여야 함")
        for item_id in covers:
            if not isinstance(item_id, str):
                raise DocdbError("BAD_ARGS", "covers 요소는 문자열이어야 함")
            if _execute(con, "SELECT 1 FROM item WHERE item_id=?", (item_id,)).fetchone() is None:
                raise DocdbError("NOT_FOUND", f"항목을 찾을 수 없음: {item_id}")

    findings = payload.get("findings")
    if findings is not None:
        if not isinstance(findings, list):
            raise DocdbError("BAD_ARGS", "findings는 리스트여야 함")
        for finding in findings:
            if not isinstance(finding, dict):
                raise DocdbError("BAD_ARGS", "finding은 dict여야 함")
            for fk in ("code", "severity", "target_type", "target_id", "message"):
                if fk not in finding or finding[fk] is None:
                    raise DocdbError("BAD_ARGS", f"finding 필수 키 누락: {fk}")
            if finding["severity"] not in _ALLOWED_SEVERITIES:
                raise DocdbError("BAD_ARGS", f"허용되지 않은 severity: {finding['severity']}")
            if finding["target_type"] not in _ALLOWED_TARGET_TYPES:
                raise DocdbError("BAD_ARGS", f"허용되지 않은 target_type: {finding['target_type']}")
            disp = finding.get("disposition", "open")
            if disp not in _ALLOWED_DISPOSITIONS:
                raise DocdbError("BAD_ARGS", f"허용되지 않은 disposition: {disp}")

    raw_log = payload.get("raw_log")
    if raw_log is not None:
        if not isinstance(raw_log, dict) or not raw_log.get("uri") or not isinstance(raw_log.get("uri"), str):
            raise DocdbError("BAD_ARGS", "raw_log uri 필수")

    trust_source = "ai_reported" if actor.type == "ai" else payload["trust_source"]
    if trust_source not in _ALLOWED_TRUST_SOURCES:
        raise DocdbError("BAD_ARGS", f"허용되지 않은 trust_source: {trust_source}")

    with db.write_tx(con):
        run_id = ids.next_id(con, "run")

        target_ref = textnorm.normalize_text(payload["target_ref"]) or payload["target_ref"]
        runner = textnorm.normalize_text(payload["runner"]) or payload["runner"]

        limitations = payload.get("limitations")
        if limitations is not None:
            limitations = textnorm.normalize_text(limitations) or limitations

        command_argv = payload.get("command_argv")
        command_argv_str = (
            textnorm.canonical_json(command_argv) if command_argv is not None else None
        )

        target_versions = payload.get("target_versions", {})
        target_versions_str = (
            textnorm.canonical_json(target_versions)
            if isinstance(target_versions, (dict, list))
            else "{}"
        )

        env_snapshot = payload.get("env_snapshot", {})
        env_snapshot_str = (
            textnorm.canonical_json(env_snapshot)
            if isinstance(env_snapshot, (dict, list))
            else "{}"
        )

        raw_uri = None
        raw_digest = None
        if raw_log is not None:
            raw_uri = textnorm.normalize_text(raw_log["uri"]) or raw_log["uri"]
            raw_digest = raw_log.get("digest")

        attestation_ref = payload.get("attestation_ref")
        if attestation_ref is not None:
            attestation_ref = textnorm.normalize_text(attestation_ref) or attestation_ref

        secret_texts: list[str | None] = [
            target_ref,
            runner,
            target_versions_str,
            env_snapshot_str,
        ]
        if limitations is not None:
            secret_texts.append(limitations)
        if raw_uri is not None:
            secret_texts.append(raw_uri)
        if command_argv_str is not None:
            secret_texts.append(command_argv_str)
        if attestation_ref is not None:
            secret_texts.append(attestation_ref)

        if findings:
            for finding in findings:
                norm_msg = textnorm.normalize_text(finding.get("message"))
                if norm_msg is not None:
                    secret_texts.append(norm_msg)
                if finding.get("evidence") is not None:
                    norm_ev = textnorm.normalize_text(finding.get("evidence"))
                    if norm_ev is not None:
                        secret_texts.append(norm_ev)

        policy.assert_no_secrets(con, f"r:{run_id}", secret_texts)

        raw_artifact_id = None
        if raw_log is not None:
            raw_artifact_id = ids.next_id(con, "artifact")
            _execute(
                con,
                "INSERT INTO artifact (artifact_id, uri, repo, commit_sha, digest, role, "
                "source_mode, external_version, access_policy_ref, retention_policy_ref) "
                "VALUES (?, ?, NULL, NULL, ?, 'evidence', 'external_reference', NULL, NULL, NULL)",
                (raw_artifact_id, raw_uri, raw_digest),
            )
            after_art = {
                "artifact_id": raw_artifact_id,
                "uri": raw_uri,
                "repo": None,
                "commit_sha": None,
                "digest": raw_digest,
                "role": "evidence",
                "source_mode": "external_reference",
                "external_version": None,
                "access_policy_ref": None,
                "retention_policy_ref": None,
            }
            history.append_change(
                con, "artifact", raw_artifact_id, "insert", None, after_art, actor.id, work_id, None
            )

        recorded_at = clock.now_iso()
        _execute(
            con,
            "INSERT INTO run (run_id, work_id, run_type, target_ref, target_versions, "
            "env_item_id, env_snapshot, runner, command_argv, started_at, ended_at, "
            "exit_code, result, raw_artifact_id, limitations, trust_source, "
            "attestation_ref, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                work_id,
                payload["run_type"],
                target_ref,
                target_versions_str,
                env_item_id,
                env_snapshot_str,
                runner,
                command_argv_str,
                payload.get("started_at"),
                payload.get("ended_at"),
                payload.get("exit_code"),
                result,
                raw_artifact_id,
                limitations,
                trust_source,
                attestation_ref,
                recorded_at,
            ),
        )

        after_run = {
            "run_id": run_id,
            "work_id": work_id,
            "run_type": payload["run_type"],
            "target_ref": target_ref,
            "target_versions": target_versions,
            "env_item_id": env_item_id,
            "env_snapshot": env_snapshot,
            "runner": runner,
            "command_argv": command_argv,
            "started_at": payload.get("started_at"),
            "ended_at": payload.get("ended_at"),
            "exit_code": payload.get("exit_code"),
            "result": result,
            "raw_artifact_id": raw_artifact_id,
            "limitations": limitations,
            "trust_source": trust_source,
            "attestation_ref": attestation_ref,
            "recorded_at": recorded_at,
        }
        history.append_change(
            con, "run", run_id, "insert", None, after_run, actor.id, work_id, None
        )

        if findings:
            for finding in findings:
                code = finding["code"]
                severity = finding["severity"]
                target_type = finding["target_type"]
                target_id = finding["target_id"]
                norm_msg = textnorm.normalize_text(finding["message"]) or finding["message"]
                norm_ev = (
                    textnorm.normalize_text(finding["evidence"])
                    if finding.get("evidence") is not None
                    else None
                )
                disp = finding.get("disposition", "open")
                disp_ref = finding.get("disposition_ref")

                cursor = _execute(
                    con,
                    "INSERT INTO finding (run_id, code, severity, target_type, target_id, "
                    "message, evidence, disposition, disposition_ref) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, code, severity, target_type, target_id, norm_msg, norm_ev, disp, disp_ref),
                )
                finding_id = cursor.lastrowid
                after_finding = {
                    "finding_id": finding_id,
                    "run_id": run_id,
                    "code": code,
                    "severity": severity,
                    "target_type": target_type,
                    "target_id": target_id,
                    "message": norm_msg,
                    "evidence": norm_ev,
                    "disposition": disp,
                    "disposition_ref": disp_ref,
                }
                history.append_change(
                    con, "finding", str(finding_id), "insert", None, after_finding, actor.id, work_id, None
                )

        if covers:
            relations.apply_run_coverage(con, actor, run_id, covers)

        return run_id
