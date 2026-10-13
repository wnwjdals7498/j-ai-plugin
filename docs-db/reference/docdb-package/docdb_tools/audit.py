"""스키마 커버리지 검수: 표준 문서의 데이터 요구 → 스키마 테이블.컬럼 매핑 존재 여부"""
import sys, re, json, collections, sqlite3
from load_catalogue import load

db, cat, schema = sys.argv[1:4]
con, d = load(db, cat, schema)
cols = {}
for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')"):
    cols[t] = {r[1] for r in con.execute(f'PRAGMA table_info({t})')}

def exists(target):
    if target is None: return False
    for part in target.split('+'):
        t, _, c = part.strip().partition('.')
        if t not in cols or (c and c not in cols[t]): return False
    return True

# 1) 공통 메타데이터 5.1 (standard.md L379-403) + 5.2 (L409-416)
META = {
 'schema_version':'cat_version.schema_version', 'doc_id':'document.doc_id', 'kind_ids':'document_kind.kind_id',
 'title':'document.title', 'language':'document.language', 'lifecycle':'document.lifecycle',
 'category':'document.category', 'status':'document.status', 'revision':'doc_revision.revision_no',
 'authority':'document.authority', 'baseline_mode':'document.baseline_mode', 'source_mode':'document.source_mode',
 'owner_role':'cat_kind.owner_role', 'owner_id':'document.owner_id', 'reviewer_ids':'doc_reviewer.actor_id',
 'scope':'doc_scope.item_id', 'classification':'document.classification', 'export_policy':'document.export_policy',
 'files':'doc_artifact.artifact_id', 'relations':'relation.rel_type', 'field_bindings':'field_value.ref_locator',
 'created_at':'document.created_at', 'updated_at':'document.updated_at',
 'review.last_reviewed_at':'document.last_reviewed_at', 'review.interval_days':'document.review_interval_days',
 'review.triggers':'document.review_triggers', 'approval_refs':'approval.revision_id',
 'retention_policy_ref':'document.retention_policy_ref', 'brief_ref':'generation.output_uri',
 'applicability.status':'document.applicability_status', 'applicability.reason':'document.applicability_reason',
 'applicability.profile_ref':'document.applicability_profile_rev',
 # 5.2
 'work.work_id':'document.work_id', 'work.work_type':'work.work_type', 'work.base_refs':'work.base_refs',
 'work.result_refs':'work.result_refs',
 'generation.generator':'generation.generator', 'generation.version':'generation.generator_version',
 'generation.inputs':'generation.input_digests',
 'external.object_id':'artifact.uri', 'external.version':'artifact.external_version',
 'external.access_policy_ref':'artifact.access_policy_ref',
 'diagram.model_files':'relation.rel_type', 'diagram.renderer':'generation.generator', 'diagram.version':'generation.generator_version',
 'superseded_by':'document.superseded_by', 'archive_reason':'document.archive_reason',
 'brief_exemption.reason':'document.brief_exemption_reason', 'brief_exemption.parent_doc_id':'document.brief_parent_doc_id',
 'tags':'doc_tag.tag', 'aliases':'doc_tag.tag', 'origin_work_id':'document.origin_work_id',
 'external_links':'relation.to_id', 'legal_hold':'document.legal_hold', 'extensions':'document.extensions',
}
# 2) 증거 9.4 (L831-837), 인계 9.3 (L797-822), 작업 9.1 (L769-772)
EVIDENCE = {
 'target(commit/artifact digest)':'run.target_ref', 'data/schema/prompt version':'run.target_versions',
 'environment id':'run.env_item_id', 'runtime/dependency/config snapshot':'run.env_snapshot',
 'run id':'run.run_id', 'runner':'run.runner', 'command argv':'run.command_argv', 'started/ended':'run.started_at+run.ended_at',
 'exit code':'run.exit_code', 'result 5-state':'run.result', 'raw artifact uri/hash':'run.raw_artifact_id',
 'retention':'artifact.retention_policy_ref', 'limitations':'run.limitations', 'trust/attestation':'run.trust_source+run.attestation_ref',
}
HANDOFF = {
 'handoff_id':'handoff.handoff_id', 'work_id':'handoff.work_id', 'from/to role':'handoff.from_role+handoff.to_role',
 'status':'handoff.ack_status', 'base/result refs':'handoff.payload', 'changed_artifacts':'handoff.payload',
 'completed_acceptance_ids':'handoff.payload', 'verification.evidence_refs':'relation.rel_type',
 'not_run':'handoff.payload', 'open_issues/risks/next_actions':'handoff.payload',
 'receiver_ack.checked_baseline':'handoff.ack_checked_baseline',
}
# 3) docctl status 질문 10.3 (L917-924) / 오류코드 10.5 (L974-985)
STATUS = {
 'check findings & codes (DOC_MISSING..)':'finding.code', 'finding severity':'finding.severity',
 'finding disposition':'finding.disposition', 'readiness gate result':'run.run_type',
 'brief freshness':'generation.freshness', 'pending approvals':'approval.decision',
}
def report(name, m):
    gaps = [k for k, v in m.items() if not exists(v)]
    print(f'[{name}] {len(m)-len(gaps)}/{len(m)} mapped; gaps={gaps}')
    return gaps
G = {}
for n, m in (('meta 5.1/5.2', META), ('evidence 9.4', EVIDENCE), ('handoff 9.3', HANDOFF), ('status/check 10.3-10.5', STATUS)):
    G[n] = report(n, m)

# 4) 카탈로그 필수 필드 897개의 저장 위치 분류
RULES = [
 ('item',     lambda k,desc: bool(re.search(r'(^|_)ids?$', k)) or re.search(r'\bID\b|ID·|·ID|코드', desc)),
 ('approval', lambda k,desc: k in ('approval','approvals','authorization','acceptance_approval') or '승인' in desc and k.startswith('approv')),
 ('actor',    lambda k,desc: k in ('owners','owner','accountable','ownership','participants','roles','delegation')),
 ('run/evidence', lambda k,desc: k in ('evidence','results','execution','checks','findings','baseline','environment','target','verification_results') or '증거' in desc),
 ('relation', lambda k,desc: k in ('dependencies','edges','nodes','successor','links','lineage','mapping') or '의존' in desc),
]
c = collections.Counter(); per = collections.defaultdict(list)
for k in d['document_types']:
    for fk, desc in k['required_fields'].items():
        tgt = next((n for n, f in RULES if f(fk, desc)), 'field_value')
        c[tgt] += 1; per[tgt].append(f"{k['kind_id']}.{fk}")
print('[required fields 897 → 저장 위치]', dict(c))
print('  item 예:', per['item'][:25])
print('  relation 예:', per['relation'][:15])

# 5) covered_names 중복 (서로 다른 종류에 같은 이름 → 이관 매핑 모호)
dup = con.execute('SELECT name, group_concat(kind_id) FROM cat_kind_alias GROUP BY name HAVING count(*)>1').fetchall()
print(f'[alias 768] 중복 이름 {len(dup)}개:', dup[:12])

# 6) 종류 간 필수 링크 대상이 조건부/선택인 경우 (N/A 사유 필요 대상)
rows = con.execute('''SELECT l.kind_id, l.target_kind_id, t.mode FROM cat_kind_link l JOIN cat_kind t
                      ON t.kind_id=l.target_kind_id WHERE l.is_required=1 AND t.mode<>'mandatory' ''').fetchall()
print(f'[required links 194] 대상이 비필수 종류인 링크 {len(rows)}개 (N/A 근거 기록 필요)')

# 7) storage class
EXTRA = {
 'link N/A reason (6.1)':'link_exemption.reason', 'kind applicability (status 10.3)':'kind_applicability.required',
 'RACI single accountable (B03)':'role_assignment.raci', 'item ID prefix rule':'cat_item_type.id_prefix',
 'hardware inventory (K02)':'item.item_type+cat_item_type.defined_by_kind', 'observed vs desired (K07/S04)':'finding.code+run.env_snapshot',
 'profile revision':'project.profile_revision', 'alias ambiguity':'cat_kind_alias.is_primary',
}
G['extra'] = report('extra', EXTRA)
print('[storage class]', con.execute('SELECT storage_class, count(*), group_concat(kind_id) FROM cat_kind GROUP BY 1').fetchall())
json.dump({'gaps': G, 'field_targets': dict(c), 'dup_alias': len(dup)}, open(db + '.audit.json', 'w'), ensure_ascii=False)
