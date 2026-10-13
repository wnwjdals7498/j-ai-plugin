"""원본 매핑 검수의 94개 항목을 현재 스키마에 적용한다."""


META = {
    "schema_version": "cat_version.schema_version",
    "doc_id": "document.doc_id",
    "kind_ids": "document_kind.kind_id",
    "title": "document.title",
    "language": "document.language",
    "lifecycle": "document.lifecycle",
    "category": "document.category",
    "status": "document.status",
    "revision": "doc_revision.revision_no",
    "authority": "document.authority",
    "baseline_mode": "document.baseline_mode",
    "source_mode": "document.source_mode",
    "owner_role": "cat_kind.owner_role",
    "owner_id": "document.owner_id",
    "reviewer_ids": "doc_reviewer.actor_id",
    "scope": "doc_scope.item_id",
    "classification": "document.classification",
    "export_policy": "document.export_policy",
    "files": "doc_artifact.artifact_id",
    "relations": "relation.rel_type",
    "field_bindings": "field_value.ref_locator",
    "created_at": "document.created_at",
    "updated_at": "document.updated_at",
    "review.last_reviewed_at": "document.last_reviewed_at",
    "review.interval_days": "document.review_interval_days",
    "review.triggers": "document.review_triggers",
    "approval_refs": "approval.revision_id",
    "retention_policy_ref": "document.retention_policy_ref",
    "brief_ref": "generation.output_uri",
    "applicability.status": "document.applicability_status",
    "applicability.reason": "document.applicability_reason",
    "applicability.profile_ref": "document.applicability_profile_rev",
    "work.work_id": "document.work_id",
    "work.work_type": "work.work_type",
    "work.base_refs": "work.base_refs",
    "work.result_refs": "work.result_refs",
    "generation.generator": "generation.generator",
    "generation.version": "generation.generator_version",
    "generation.inputs": "generation.input_digests",
    "external.object_id": "artifact.uri",
    "external.version": "artifact.external_version",
    "external.access_policy_ref": "artifact.access_policy_ref",
    "diagram.model_files": "relation.rel_type",
    "diagram.renderer": "generation.generator",
    "diagram.version": "generation.generator_version",
    "superseded_by": "document.superseded_by",
    "archive_reason": "document.archive_reason",
    "brief_exemption.reason": "document.brief_exemption_reason",
    "brief_exemption.parent_doc_id": "document.brief_parent_doc_id",
    "tags": "doc_tag.tag",
    "aliases": "doc_tag.tag",
    "origin_work_id": "document.origin_work_id",
    "external_links": "relation.to_id",
    "legal_hold": "document.legal_hold",
    "extensions": "document.extensions",
}

EVIDENCE = {
    "target(commit/artifact digest)": "run.target_ref",
    "data/schema/prompt version": "run.target_versions",
    "environment id": "run.env_item_id",
    "runtime/dependency/config snapshot": "run.env_snapshot",
    "run id": "run.run_id",
    "runner": "run.runner",
    "command argv": "run.command_argv",
    "started/ended": "run.started_at+run.ended_at",
    "exit code": "run.exit_code",
    "result 5-state": "run.result",
    "raw artifact uri/hash": "run.raw_artifact_id",
    "retention": "artifact.retention_policy_ref",
    "limitations": "run.limitations",
    "trust/attestation": "run.trust_source+run.attestation_ref",
}

HANDOFF = {
    "handoff_id": "handoff.handoff_id",
    "work_id": "handoff.work_id",
    "from/to role": "handoff.from_role+handoff.to_role",
    "status": "handoff.ack_status",
    "base/result refs": "handoff.payload",
    "changed_artifacts": "handoff.payload",
    "completed_acceptance_ids": "handoff.payload",
    "verification.evidence_refs": "relation.rel_type",
    "not_run": "handoff.payload",
    "open_issues/risks/next_actions": "handoff.payload",
    "receiver_ack.checked_baseline": "handoff.ack_checked_baseline",
}

STATUS = {
    "check findings & codes (DOC_MISSING..)": "finding.code",
    "finding severity": "finding.severity",
    "finding disposition": "finding.disposition",
    "readiness gate result": "run.run_type",
    "brief freshness": "generation.freshness",
    "pending approvals": "approval.decision",
}

EXTRA = {
    "link N/A reason (6.1)": "link_exemption.reason",
    "kind applicability (status 10.3)": "kind_applicability.required",
    "RACI single accountable (B03)": "role_assignment.raci",
    "item ID prefix rule": "cat_item_type.id_prefix",
    "hardware inventory (K02)": "item.item_type+cat_item_type.defined_by_kind",
    "observed vs desired (K07/S04)": "finding.code+run.env_snapshot",
    "profile revision": "project.profile_revision",
    "alias ambiguity": "cat_kind_alias.is_primary",
}


def _exists(target, columns):
    """표현된 모든 테이블과 컬럼이 현재 스키마에 있는지 확인한다."""
    if target is None:
        return False
    for part in target.split("+"):
        table, _, column = part.strip().partition(".")
        if table not in columns or (column and column not in columns[table]):
            return False
    return True


def test_t13_34_audit_mapping_groups_are_fully_mapped(project_db):
    from docdb import db

    con = db.connect(project_db)
    try:
        columns = {}
        for (table,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ):
            columns[table] = {
                row[1] for row in con.execute(f"PRAGMA table_info({table})")
            }

        groups = (META, EVIDENCE, HANDOFF, STATUS, EXTRA)
        mapped = tuple(
            sum(1 for target in group.values() if _exists(target, columns))
            for group in groups
        )
        assert tuple(map(len, groups)) == (55, 14, 11, 6, 8)
        assert mapped == (55, 14, 11, 6, 8)
    finally:
        con.close()
