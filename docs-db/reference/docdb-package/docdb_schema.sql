-- =====================================================================
-- docdb_schema.sql — 문서 데이터 DB 스키마 초안 (SQLite 3.38+ 기준, PostgreSQL 이식 고려)
-- 근거: project-documentation-standard.md (DOC-STD-001), project-documentation-catalogue.yaml
-- 원칙
--   1) 구조·서술·관계·이력은 DB가 원본(SoT). 문서(md/html/brief)는 생성물.
--   2) 코드·계약(OpenAPI/AsyncAPI)·Migration·IaC는 Git이 원본. DB는 경로+커밋+digest로 참조만.
--   3) 모든 쓰기는 스크립트(docctl)를 통해서만 → change_log 자동 기록.
--   4) 이력 테이블은 append-only. 수정·삭제 금지, 정정은 새 행으로 추가.
-- 계층
--   R. 규칙(카탈로그)   cat_*        : catalogue.yaml 로드. 스크립트 전용
--   P. 프로젝트 프로필  project, fact_value, kind_selection, policy_exception
--   I. 실체             actor, document, field_value, item, artifact, relation
--   W. 작업·증거        work, work_event, run, approval, handoff
--   H. 이력             change_log, doc_revision  (링크드 리스트 + 순번 + 해시 체인)
--   D. 파생 기록        generation   (생성물 자체는 파일, 여기엔 생성 사실만)
-- =====================================================================
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- R. 규칙 계층 (catalogue.yaml 1:1 적재, 카탈로그 버전 단위로 교체)
-- ---------------------------------------------------------------------
CREATE TABLE cat_version (
  catalogue_version TEXT PRIMARY KEY,          -- standard_version 예: 1.0.0-draft.1
  schema_version    TEXT NOT NULL,
  source_digest     TEXT NOT NULL,             -- catalogue.yaml sha256
  loaded_at         TEXT NOT NULL
);

CREATE TABLE cat_group (                        -- A~Z 26개 영역
  group_code         TEXT PRIMARY KEY CHECK (length(group_code) = 1),
  directory          TEXT NOT NULL UNIQUE,
  title_ko           TEXT NOT NULL,
  title_en           TEXT NOT NULL,
  default_owner_role TEXT NOT NULL,
  review_days        INTEGER
);

CREATE TABLE cat_fact (                         -- cap.* 38개 / evt.* 53개
  fact_key     TEXT PRIMARY KEY,                -- 'cap.api', 'evt.release'
  fact_type    TEXT NOT NULL CHECK (fact_type IN ('cap','evt')),
  description  TEXT NOT NULL,
  derived_rule TEXT CHECK (derived_rule IS NULL OR json_valid(derived_rule))  -- cap.integration = OR(...)
);

CREATE TABLE cat_kind (                         -- 문서 종류 194개
  kind_id       TEXT PRIMARY KEY,               -- 'D01'
  abbreviation  TEXT NOT NULL UNIQUE,           -- 'REQ-SPEC'
  title_ko      TEXT NOT NULL,
  title_en      TEXT NOT NULL,
  group_code    TEXT NOT NULL REFERENCES cat_group(group_code),
  lifecycle     TEXT NOT NULL CHECK (lifecycle IN ('living','work')),
  mode          TEXT NOT NULL CHECK (mode IN ('mandatory','conditional','optional')),
  storage_class TEXT NOT NULL DEFAULT 'narrative'
                CHECK (storage_class IN ('narrative','register','view','run')),
                -- narrative: 서술 필드(field_value) / register: 항목 대장(item 행)
                -- view: 쿼리로 생성(작성 안 함) / run: 실행 기록(run 행)
  owner_role    TEXT NOT NULL,
  review_days   INTEGER,
  trigger_text  TEXT,
  required_when TEXT NOT NULL CHECK (json_valid(required_when)),  -- 제한 연산만: always/fact/all/any/selected_kind
  applies_when  TEXT NOT NULL CHECK (json_valid(applies_when)),
  content_files TEXT NOT NULL CHECK (json_valid(content_files))   -- 생성 시 출력 파일 구성
);

CREATE TABLE cat_kind_field (                   -- 필수 897 / 선택 387
  kind_id        TEXT NOT NULL REFERENCES cat_kind(kind_id),
  field_key      TEXT NOT NULL,
  is_required    INTEGER NOT NULL CHECK (is_required IN (0,1)),
  description    TEXT NOT NULL,
  brief_preserve INTEGER NOT NULL DEFAULT 0 CHECK (brief_preserve IN (0,1)),
  sort_order     INTEGER NOT NULL,              -- 생성 문서의 절 순서
  PRIMARY KEY (kind_id, field_key)
);

CREATE TABLE cat_kind_link (                    -- 필수 194 / 선택 575 (종류 간 연결 규칙)
  kind_id        TEXT NOT NULL REFERENCES cat_kind(kind_id),
  target_kind_id TEXT NOT NULL REFERENCES cat_kind(kind_id),
  is_required    INTEGER NOT NULL CHECK (is_required IN (0,1)),
  PRIMARY KEY (kind_id, target_kind_id)
);

CREATE TABLE cat_kind_alias (                   -- covered_names 768개: 검색·이관 매핑용
  kind_id    TEXT NOT NULL REFERENCES cat_kind(kind_id),
  name       TEXT NOT NULL,
  is_primary INTEGER NOT NULL DEFAULT 1 CHECK (is_primary IN (0,1)),  -- 같은 이름이 여러 종류에 있을 때 대표 1개
  PRIMARY KEY (kind_id, name)
);

CREATE TABLE cat_item_type (                    -- [확장 제안] 카탈로그에 없음. 고정 ID 항목의 종류·접두어 규칙
  item_type      TEXT PRIMARY KEY,              -- 'requirement'
  id_prefix      TEXT NOT NULL UNIQUE,          -- 'REQ-'
  defined_by_kind TEXT NOT NULL REFERENCES cat_kind(kind_id),   -- 이 항목의 대장(정의 문서) 종류
  description    TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- P. 프로젝트 프로필 (profile.yaml 대체)
-- ---------------------------------------------------------------------
CREATE TABLE project (
  project_id        TEXT PRIMARY KEY,           -- 'PRJ-xxx'
  name              TEXT NOT NULL,
  catalogue_version TEXT NOT NULL REFERENCES cat_version(catalogue_version),
  profile_status    TEXT NOT NULL DEFAULT 'draft' CHECK (profile_status IN ('draft','approved')),
  profile_revision  INTEGER NOT NULL DEFAULT 1,  -- 사실·선택이 바뀌어 승인될 때 +1 (applicability 근거 버전)
  retention_policy_ref TEXT
);

CREATE TABLE kind_applicability (               -- [파생 캐시] 규칙 평가 결과. 스크립트가 재계산
  project_id  TEXT NOT NULL REFERENCES project(project_id),
  kind_id     TEXT NOT NULL REFERENCES cat_kind(kind_id),
  applies     TEXT NOT NULL CHECK (applies IN ('true','false','unknown')),
  required    TEXT NOT NULL CHECK (required IN ('true','false','unknown')),
  reason      TEXT NOT NULL,                    -- 평가에 쓰인 사실 목록
  profile_revision INTEGER NOT NULL,
  evaluated_at TEXT NOT NULL,
  PRIMARY KEY (project_id, kind_id)
);

CREATE TABLE fact_value (                       -- 세 값 논리: 1 참 / 0 거짓 / NULL 미확인
  project_id   TEXT NOT NULL REFERENCES project(project_id),
  fact_key     TEXT NOT NULL REFERENCES cat_fact(fact_key),
  value        INTEGER CHECK (value IS NULL OR value IN (0,1)),
  reason       TEXT,
  evidence_ref TEXT,
  confirmed_by TEXT REFERENCES actor(actor_id),
  confirmed_at TEXT,
  PRIMARY KEY (project_id, fact_key),
  CHECK (fact_key LIKE 'cap.%')                 -- evt.* 는 작업 단위(work_event)에서 관리
);

CREATE TABLE kind_selection (                   -- selected_kinds / promoted_kinds
  project_id TEXT NOT NULL REFERENCES project(project_id),
  kind_id    TEXT NOT NULL REFERENCES cat_kind(kind_id),
  selection  TEXT NOT NULL CHECK (selection IN ('selected','promoted')),
  reason     TEXT NOT NULL,
  PRIMARY KEY (project_id, kind_id)
);

CREATE TABLE policy_exception (
  exception_id TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES project(project_id),
  kind_id      TEXT REFERENCES cat_kind(kind_id),
  scope        TEXT NOT NULL,
  reason       TEXT NOT NULL,
  approved_by  TEXT NOT NULL REFERENCES actor(actor_id),
  expires_at   TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- I. 실체 계층
-- ---------------------------------------------------------------------
CREATE TABLE actor (                            -- 사람·팀·AI 모델. AI는 최종 책임자/승인자 불가
  actor_id   TEXT PRIMARY KEY,                  -- 'team-backend', 'u-jjm', 'ai-codex-x'
  actor_type TEXT NOT NULL CHECK (actor_type IN ('human','team','ai')),
  name       TEXT NOT NULL,
  active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE work (                             -- WORK-YYYY-NNNN
  work_id    TEXT PRIMARY KEY CHECK (work_id GLOB 'WORK-[0-9][0-9][0-9][0-9]-[0-9]*'),
  project_id TEXT NOT NULL REFERENCES project(project_id),
  work_type  TEXT NOT NULL,                     -- feature/bugfix/release/incident/migration/audit/...
  title      TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'planned'
             CHECK (status IN ('planned','active','blocked','review','verified','closed','cancelled')),
  owner_id   TEXT NOT NULL REFERENCES actor(actor_id),
  base_refs  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(base_refs)),   -- 기준 커밋·doc_revision
  result_refs TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(result_refs)), -- 결과 커밋·산출물
  opened_at  TEXT NOT NULL,
  closed_at  TEXT
);

CREATE TABLE document (                         -- 논리 문서 묶음 (meta.yaml 대체)
  doc_id         TEXT PRIMARY KEY,              -- 'DOC-REQ-ORDERS' 불변, 재사용 금지
  project_id     TEXT NOT NULL REFERENCES project(project_id),
  title          TEXT NOT NULL,
  language       TEXT NOT NULL DEFAULT 'ko',
  lifecycle      TEXT NOT NULL CHECK (lifecycle IN ('living','work')),
  category       TEXT NOT NULL REFERENCES cat_group(group_code),
  status         TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft','in_review','approved','superseded','archived')),
  authority      TEXT NOT NULL CHECK (authority IN ('normative','evidence','derived')),
  baseline_mode  TEXT NOT NULL CHECK (baseline_mode IN ('desired','observed','historical')),
  source_mode    TEXT NOT NULL DEFAULT 'authored'
                 CHECK (source_mode IN ('authored','generated','external_reference')),
  owner_id       TEXT NOT NULL REFERENCES actor(actor_id),
  classification TEXT NOT NULL DEFAULT 'internal'
                 CHECK (classification IN ('public','internal','confidential','restricted')),
  export_policy  TEXT NOT NULL DEFAULT 'local_only'
                 CHECK (export_policy IN ('public','approved_providers','local_only')),
  work_id        TEXT REFERENCES work(work_id),
  origin_work_id TEXT REFERENCES work(work_id),   -- living 문서를 만든/바꾼 작업
  superseded_by  TEXT REFERENCES document(doc_id),
  archive_reason TEXT,
  legal_hold     INTEGER NOT NULL DEFAULT 0 CHECK (legal_hold IN (0,1)),
  retention_policy_ref TEXT,
  -- review (5.1)
  last_reviewed_at     TEXT,                       -- 실제 의미 검토한 경우에만 갱신
  review_interval_days INTEGER,                    -- NULL이면 cat_kind.review_days 사용 (override만 저장)
  review_triggers      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(review_triggers)),
  -- applicability (5.1)
  applicability_status TEXT NOT NULL DEFAULT 'unknown'
                       CHECK (applicability_status IN ('applicable','not_applicable','unknown')),
  applicability_reason TEXT,
  applicability_profile_rev INTEGER,
  -- brief exemption (5.2)
  brief_exemption_reason TEXT,
  brief_parent_doc_id    TEXT REFERENCES document(doc_id),
  extensions     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(extensions)),
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  CHECK ((lifecycle = 'work') = (work_id IS NOT NULL)),  -- work 문서는 작업에 반드시 소속
  CHECK (status <> 'archived' OR archive_reason IS NOT NULL),
  CHECK (status <> 'superseded' OR superseded_by IS NOT NULL),
  CHECK (brief_exemption_reason IS NULL OR brief_parent_doc_id IS NOT NULL)
);
-- owner_role, review_days 는 저장하지 않음: cat_kind / cat_group 기본값에서 계산 (중복 제거)

CREATE TABLE doc_tag (                          -- tags / aliases (5.2 선택)
  doc_id   TEXT NOT NULL REFERENCES document(doc_id),
  tag      TEXT NOT NULL,
  tag_type TEXT NOT NULL DEFAULT 'tag' CHECK (tag_type IN ('tag','alias')),
  PRIMARY KEY (doc_id, tag, tag_type)
);

CREATE TABLE document_kind (                    -- kind_ids (통합 문서는 여러 개)
  doc_id  TEXT NOT NULL REFERENCES document(doc_id),
  kind_id TEXT NOT NULL REFERENCES cat_kind(kind_id),
  PRIMARY KEY (doc_id, kind_id)
);

CREATE TABLE doc_reviewer (
  doc_id   TEXT NOT NULL REFERENCES document(doc_id),
  actor_id TEXT NOT NULL REFERENCES actor(actor_id),
  PRIMARY KEY (doc_id, actor_id)
);

CREATE TABLE field_value (                      -- 본문 = 필드 단위 데이터 (detail.md + field_bindings 대체)
  doc_id     TEXT NOT NULL,
  kind_id    TEXT NOT NULL,
  field_key  TEXT NOT NULL,
  fill_state TEXT NOT NULL CHECK (fill_state IN ('filled','tbd','unknown','not_applicable')),
  format     TEXT NOT NULL DEFAULT 'md' CHECK (format IN ('md','json','ref')),
  value_text TEXT,                              -- format=md
  value_json TEXT CHECK (value_json IS NULL OR json_valid(value_json)),   -- format=json
  ref_artifact_id TEXT REFERENCES artifact(artifact_id),               -- format=ref (Git 원본의 절/포인터)
  ref_locator TEXT,                             -- '#behavior' 또는 '/criteria'
  reason     TEXT,                              -- tbd/unknown/not_applicable 사유 필수
  author_id  TEXT NOT NULL REFERENCES actor(actor_id),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (doc_id, kind_id, field_key),
  FOREIGN KEY (doc_id, kind_id) REFERENCES document_kind(doc_id, kind_id),
  FOREIGN KEY (kind_id, field_key) REFERENCES cat_kind_field(kind_id, field_key),
  CHECK (fill_state = 'filled' OR reason IS NOT NULL),
  CHECK (format <> 'ref' OR ref_artifact_id IS NOT NULL)
);

CREATE TABLE item (                             -- 고정 ID 항목: REQ/AC/SCR/API/TEST/ERR/RISK/TERM/SVC/FEAT/ENV/HW/...
  item_id    TEXT PRIMARY KEY,                  -- 'REQ-ORD-001', 'HW-DB01', 'ENV-PROD'
  project_id TEXT NOT NULL REFERENCES project(project_id),
  item_type  TEXT NOT NULL REFERENCES cat_item_type(item_type),
  title      TEXT NOT NULL,
  status     TEXT,
  defined_in TEXT REFERENCES document(doc_id),  -- 이 항목을 정의하는 문서(대장)
  attrs      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(attrs)),   -- 기대(desired) 속성. 관측값은 run/finding
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TRIGGER item_id_prefix BEFORE INSERT ON item
WHEN NEW.item_id NOT LIKE (SELECT id_prefix FROM cat_item_type WHERE item_type = NEW.item_type) || '%'
BEGIN SELECT RAISE(ABORT, 'item_id must start with the item_type prefix'); END;

CREATE TABLE role_assignment (                  -- B03 RACI: 역할(item) ↔ 실제 담당(actor)
  role_item_id TEXT NOT NULL REFERENCES item(item_id),
  actor_id     TEXT NOT NULL REFERENCES actor(actor_id),
  raci         TEXT NOT NULL CHECK (raci IN ('R','A','C','I')),
  valid_from   TEXT NOT NULL,
  valid_to     TEXT,
  PRIMARY KEY (role_item_id, actor_id, raci, valid_from)
);
CREATE UNIQUE INDEX ux_single_accountable ON role_assignment(role_item_id)
  WHERE raci = 'A' AND valid_to IS NULL;          -- 단일 최종 책임 (B03.accountable)

CREATE TABLE doc_scope (                        -- scope: 서비스·기능·환경·자산
  doc_id  TEXT NOT NULL REFERENCES document(doc_id),
  item_id TEXT NOT NULL REFERENCES item(item_id),
  PRIMARY KEY (doc_id, item_id)
);

CREATE TABLE artifact (                         -- Git 파일·외부 객체·원시 증거 (내용은 복제하지 않음)
  artifact_id TEXT PRIMARY KEY,
  uri         TEXT NOT NULL,                    -- 저장소 상대 POSIX 경로 또는 외부 URI
  repo        TEXT,
  commit_sha  TEXT,
  digest      TEXT,                             -- sha256:...
  role        TEXT NOT NULL CHECK (role IN ('primary','structured','contract','migration','iac','code',
                                             'design','evidence','dataset','derived')),
  source_mode TEXT NOT NULL CHECK (source_mode IN ('authored','generated','external_reference')),
  external_version TEXT,
  access_policy_ref TEXT,
  retention_policy_ref TEXT,
  CHECK (uri LIKE '%://%' OR (uri NOT LIKE '/%' AND uri NOT LIKE '%..%' AND uri NOT LIKE '%\%' AND uri NOT LIKE '_:%'))
  -- 저장소 경로는 상대 POSIX만: 절대경로·../·역슬래시·드라이브 접두사 거부 (외부 URI는 scheme 필수)
);

CREATE TABLE doc_artifact (                     -- files (5.1): 문서 ↔ 원본·구조·계약·증거 파일
  doc_id      TEXT NOT NULL REFERENCES document(doc_id),
  artifact_id TEXT NOT NULL REFERENCES artifact(artifact_id),
  file_role   TEXT NOT NULL,                    -- primary/structured/contract/evidence/derived
  PRIMARY KEY (doc_id, artifact_id)
);

CREATE TABLE relation (                         -- 모든 연결은 여기 한 곳 (RTM·의존성 맵은 이 테이블의 쿼리)
  rel_id      INTEGER PRIMARY KEY,
  from_type   TEXT NOT NULL CHECK (from_type IN ('document','item','artifact','work','run')),
  from_id     TEXT NOT NULL,
  rel_type    TEXT NOT NULL CHECK (rel_type IN ('references','implements','verified_by','constrained_by',
                 'depends_on','uses_data','visualizes','generated_from','changes','supersedes',
                 'evidence_for','handoff_to')),
  to_type     TEXT NOT NULL CHECK (to_type IN ('document','item','artifact','work','run')),
  to_id       TEXT NOT NULL,
  is_required INTEGER NOT NULL DEFAULT 0 CHECK (is_required IN (0,1)),
  target_revision_id INTEGER REFERENCES doc_revision(revision_id),
  anchor      TEXT,
  reason      TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE (from_type, from_id, rel_type, to_type, to_id)
);
-- supersedes / generated_from 순환 금지는 스크립트(check)에서 재귀 쿼리로 검사

CREATE TABLE link_exemption (                   -- 필수 연결 대상이 비적용/미확인일 때 N/A + 이유 + 프로필 근거 (6.1)
  doc_id         TEXT NOT NULL REFERENCES document(doc_id),
  target_kind_id TEXT NOT NULL REFERENCES cat_kind(kind_id),
  status         TEXT NOT NULL CHECK (status IN ('not_applicable','unknown')),
  reason         TEXT NOT NULL,
  profile_revision INTEGER NOT NULL,
  PRIMARY KEY (doc_id, target_kind_id)
);

-- ---------------------------------------------------------------------
-- W. 작업·사건·증거
-- ---------------------------------------------------------------------
CREATE TABLE work_event (                       -- evt.* 사실: 작업마다 선언
  work_id     TEXT NOT NULL REFERENCES work(work_id),
  fact_key    TEXT NOT NULL REFERENCES cat_fact(fact_key),
  value       INTEGER CHECK (value IS NULL OR value IN (0,1)),
  declared_by TEXT NOT NULL REFERENCES actor(actor_id),
  source      TEXT NOT NULL CHECK (source IN ('declared','git_diff','ci','script')),
  PRIMARY KEY (work_id, fact_key),
  CHECK (fact_key LIKE 'evt.%')
);

CREATE TABLE run (                              -- 테스트·빌드·배포·백업·복원·훈련·스캔 실행 증거 (9.4)
  run_id       TEXT PRIMARY KEY,
  work_id      TEXT REFERENCES work(work_id),
  run_type     TEXT NOT NULL,                   -- test/build/deploy/backup/restore/dr_drill/scan/hw_check/...
  target_ref   TEXT NOT NULL,                   -- commit 또는 artifact digest
  target_versions TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(target_versions)),  -- 데이터·스키마·프롬프트 버전
  env_item_id  TEXT REFERENCES item(item_id),   -- 환경·하드웨어 자산
  env_snapshot TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(env_snapshot)),        -- 런타임·의존 버전·설정(민감값 제외)
  runner       TEXT NOT NULL,
  command_argv TEXT CHECK (command_argv IS NULL OR json_valid(command_argv)),   -- 셸 문자열 금지, 배열
  started_at   TEXT,
  ended_at     TEXT,
  exit_code    INTEGER,
  result       TEXT NOT NULL CHECK (result IN ('PASS','FAIL','NOT_RUN','ERROR','UNKNOWN')),
  raw_artifact_id TEXT REFERENCES artifact(artifact_id),
  limitations  TEXT,                            -- Mock·샘플링·flaky·환경 차이
  trust_source TEXT NOT NULL CHECK (trust_source IN ('ci','runner','manual','ai_reported')),
  attestation_ref TEXT,                         -- 서명·provenance 참조
  recorded_at  TEXT NOT NULL
);

CREATE TABLE finding (                          -- 검사·리뷰·보안·드리프트 결과의 개별 지적 (A06/G07/M08/K07/S04/...)
  finding_id  INTEGER PRIMARY KEY,
  run_id      TEXT NOT NULL REFERENCES run(run_id),
  code        TEXT NOT NULL,                    -- DOC_MISSING/FIELD_MISSING/BRIEF_STALE/DRIFT/VULN/...
  severity    TEXT NOT NULL CHECK (severity IN ('info','warning','error','critical')),
  target_type TEXT NOT NULL CHECK (target_type IN ('document','item','artifact','work','field','project')),
  target_id   TEXT NOT NULL,
  message     TEXT NOT NULL,
  evidence    TEXT,
  disposition TEXT NOT NULL DEFAULT 'open'
              CHECK (disposition IN ('open','fixed','accepted_exception','false_positive','deferred')),
  disposition_ref TEXT                          -- 조치 작업·예외 승인 ID
);

CREATE TABLE approval (                         -- 승인은 사람만, 특정 리비전에 대해
  approval_id  TEXT PRIMARY KEY,
  revision_id  INTEGER REFERENCES doc_revision(revision_id),
  work_id      TEXT REFERENCES work(work_id),
  gate         TEXT,                            -- design-approval/pre-merge/pre-release/work-close/...
  approver_id  TEXT NOT NULL REFERENCES actor(actor_id),
  decision     TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
  scope_note   TEXT,
  external_ref TEXT,                            -- PR 리뷰·결재 시스템 ID
  decided_at   TEXT NOT NULL,
  CHECK (revision_id IS NOT NULL OR work_id IS NOT NULL)
);

CREATE TABLE handoff (                          -- 9.3 인수인계
  handoff_id   TEXT PRIMARY KEY,
  work_id      TEXT NOT NULL REFERENCES work(work_id),
  from_role    TEXT NOT NULL,
  to_role      TEXT NOT NULL,
  payload      TEXT NOT NULL CHECK (json_valid(payload)),   -- 완료/미완료/위험/다음 행동/not_run
  ack_status   TEXT NOT NULL DEFAULT 'pending' CHECK (ack_status IN ('pending','accepted','rejected')),
  ack_by       TEXT REFERENCES actor(actor_id),
  ack_at       TEXT,
  ack_checked_baseline TEXT,                    -- 수신자가 직접 확인한 기준 커밋
  ack_checked_evidence TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(ack_checked_evidence)),
  created_at   TEXT NOT NULL,
  CHECK (ack_status = 'pending' OR (ack_by IS NOT NULL AND ack_checked_baseline IS NOT NULL))
);

CREATE TABLE context_read (                     -- V05 컨텍스트 잠금: AI가 작업 중 '무엇의 어느 시점 값'을 읽었는지 자동 기록
  read_id        INTEGER PRIMARY KEY,
  work_id        TEXT NOT NULL REFERENCES work(work_id),
  actor_id       TEXT NOT NULL REFERENCES actor(actor_id),
  session_ref    TEXT,                          -- 에이전트 세션·실행 ID
  target_type    TEXT NOT NULL CHECK (target_type IN ('document','item','artifact')),
  target_id      TEXT NOT NULL,
  revision_id    INTEGER REFERENCES doc_revision(revision_id),
  last_change_id INTEGER REFERENCES change_log(change_id),   -- 읽은 시점의 최신 변경 (이후 변경 = 낡은 근거)
  digest         TEXT,
  is_required    INTEGER NOT NULL DEFAULT 1 CHECK (is_required IN (0,1)),
  reason         TEXT,                          -- 선정 이유
  read_at        TEXT NOT NULL
);
CREATE TRIGGER context_read_no_update BEFORE UPDATE ON context_read
BEGIN SELECT RAISE(ABORT, 'context_read is append-only'); END;

-- ---------------------------------------------------------------------
-- H. 이력 계층 — 링크드 리스트 + 순번 + 해시 체인, append-only
-- ---------------------------------------------------------------------
CREATE TABLE change_log (                       -- 모든 엔터티의 세부 변경 (누가·언제·무엇을)
  change_id      INTEGER PRIMARY KEY,
  entity_type    TEXT NOT NULL,                 -- 'document','field_value','item','relation','fact_value',...
  entity_id      TEXT NOT NULL,                 -- 복합키는 정규화 문자열 'DOC-X|D01|behavior'
  seq            INTEGER NOT NULL CHECK (seq >= 1),
  prev_change_id INTEGER UNIQUE REFERENCES change_log(change_id),   -- 링크드 리스트, UNIQUE=분기(fork) 금지
  op             TEXT NOT NULL CHECK (op IN ('insert','update','delete','correct')),
  before_json    TEXT CHECK (before_json IS NULL OR json_valid(before_json)),
  after_json     TEXT CHECK (after_json IS NULL OR json_valid(after_json)),
  actor_id       TEXT NOT NULL REFERENCES actor(actor_id),
  work_id        TEXT REFERENCES work(work_id),
  reason         TEXT,
  changed_at     TEXT NOT NULL,
  row_digest     TEXT NOT NULL,                 -- sha256(canonical json of this row + prev_digest)
  prev_digest    TEXT,                          -- 직전 행 digest → 위변조 탐지
  UNIQUE (entity_type, entity_id, seq),         -- 순회 없이 N번째 직접 조회
  CHECK ((seq = 1) = (prev_change_id IS NULL))
);

CREATE TABLE doc_revision (                     -- 문서 기준선(승인·배포 단위) 스냅샷
  revision_id      INTEGER PRIMARY KEY,
  doc_id           TEXT NOT NULL REFERENCES document(doc_id),
  revision_no      INTEGER NOT NULL CHECK (revision_no >= 1),
  prev_revision_id INTEGER UNIQUE REFERENCES doc_revision(revision_id),
  snapshot_json    TEXT NOT NULL CHECK (json_valid(snapshot_json)),   -- 메타+필드+관계 고정 사본
  content_digest   TEXT NOT NULL,               -- 5.4 규칙의 source_digest
  prev_digest      TEXT,
  last_change_id   INTEGER REFERENCES change_log(change_id),          -- 어느 변경까지 반영했는지
  reason           TEXT NOT NULL,
  created_by       TEXT NOT NULL REFERENCES actor(actor_id),
  created_at       TEXT NOT NULL,
  UNIQUE (doc_id, revision_no),
  CHECK ((revision_no = 1) = (prev_revision_id IS NULL))
);

-- append-only 강제
CREATE TRIGGER change_log_no_update BEFORE UPDATE ON change_log
BEGIN SELECT RAISE(ABORT, 'change_log is append-only; add a correct record'); END;
CREATE TRIGGER change_log_no_delete BEFORE DELETE ON change_log
BEGIN SELECT RAISE(ABORT, 'change_log is append-only'); END;
CREATE TRIGGER doc_revision_no_update BEFORE UPDATE ON doc_revision
BEGIN SELECT RAISE(ABORT, 'doc_revision is immutable; create a new revision'); END;
CREATE TRIGGER doc_revision_no_delete BEFORE DELETE ON doc_revision
BEGIN SELECT RAISE(ABORT, 'doc_revision is immutable'); END;
CREATE TRIGGER run_no_update BEFORE UPDATE ON run
BEGIN SELECT RAISE(ABORT, 'run evidence is immutable; record a new run'); END;

-- 링크는 같은 엔터티/문서 안에서만, 순번은 직전+1
CREATE TRIGGER change_log_chain_check BEFORE INSERT ON change_log
WHEN NEW.prev_change_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM change_log p
                 WHERE p.change_id = NEW.prev_change_id
                   AND p.entity_type = NEW.entity_type AND p.entity_id = NEW.entity_id
                   AND p.seq = NEW.seq - 1
                   AND p.row_digest = NEW.prev_digest)
BEGIN SELECT RAISE(ABORT, 'broken chain: prev must be same entity, seq-1, matching digest'); END;

CREATE TRIGGER doc_revision_chain_check BEFORE INSERT ON doc_revision
WHEN NEW.prev_revision_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM doc_revision p
                 WHERE p.revision_id = NEW.prev_revision_id
                   AND p.doc_id = NEW.doc_id AND p.revision_no = NEW.revision_no - 1
                   AND p.content_digest = NEW.prev_digest)
BEGIN SELECT RAISE(ABORT, 'broken chain: prev must be same doc, revision_no-1, matching digest'); END;

-- 한 문서에 living/work 종류 혼합 금지 (AT-29)
CREATE TRIGGER document_kind_lifecycle BEFORE INSERT ON document_kind
WHEN (SELECT lifecycle FROM cat_kind WHERE kind_id = NEW.kind_id)
  <> (SELECT lifecycle FROM document WHERE doc_id = NEW.doc_id)
BEGIN SELECT RAISE(ABORT, 'kind lifecycle must match document lifecycle'); END;

-- 승인자는 사람만
CREATE TRIGGER approval_human_only BEFORE INSERT ON approval
WHEN (SELECT actor_type FROM actor WHERE actor_id = NEW.approver_id) = 'ai'
BEGIN SELECT RAISE(ABORT, 'AI actor cannot approve'); END;

-- legal_hold 문서 삭제 금지
CREATE TRIGGER document_legal_hold BEFORE DELETE ON document
WHEN OLD.legal_hold = 1
BEGIN SELECT RAISE(ABORT, 'legal_hold document cannot be deleted'); END;

-- ---------------------------------------------------------------------
-- D. 파생 기록 — 생성물(md/html/brief/status.json)은 파일, 여기엔 생성 사실만
-- ---------------------------------------------------------------------
CREATE TABLE generation (
  gen_id            INTEGER PRIMARY KEY,
  output_type       TEXT NOT NULL,              -- 'doc_md','brief_en','register','rtm','status','context'
  target_id         TEXT NOT NULL,              -- doc_id / work_id / 'project'
  source_revision_id INTEGER REFERENCES doc_revision(revision_id),
  source_digest     TEXT NOT NULL,
  generator         TEXT NOT NULL,
  generator_version TEXT NOT NULL,
  input_digests     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(input_digests)),   -- generation.inputs
  author_type       TEXT NOT NULL CHECK (author_type IN ('script','llm')),   -- LLM 생성물은 검토 필요
  output_uri        TEXT NOT NULL,
  output_digest     TEXT NOT NULL,
  freshness         TEXT NOT NULL CHECK (freshness IN ('FRESH','STALE','UNVERIFIED','BLOCKED')),
  generated_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- 자주 쓰는 뷰 (문서로 '작성'하지 않고 조회로 생성하는 종류의 예)
-- ---------------------------------------------------------------------
-- A01 문서 마스터 목록
CREATE VIEW v_document_register AS
SELECT d.doc_id, group_concat(dk.kind_id) AS kind_ids, d.title, d.lifecycle, d.category,
       d.status, d.authority, d.owner_id, d.classification, d.updated_at
FROM document d LEFT JOIN document_kind dk ON dk.doc_id = d.doc_id
GROUP BY d.doc_id;

-- 필수 필드 누락 (FIELD_MISSING)
CREATE VIEW v_missing_required_fields AS
SELECT dk.doc_id, f.kind_id, f.field_key, f.description
FROM document_kind dk
JOIN cat_kind_field f ON f.kind_id = dk.kind_id AND f.is_required = 1
LEFT JOIN field_value v ON v.doc_id = dk.doc_id AND v.kind_id = f.kind_id AND v.field_key = f.field_key
JOIN cat_kind k ON k.kind_id = f.kind_id
WHERE k.storage_class = 'narrative'
  AND (v.doc_id IS NULL OR (v.fill_state = 'filled' AND coalesce(v.value_text, v.value_json, v.ref_locator) IS NULL));

-- D08 요구사항 추적 (REQ → 구현·검증)
CREATE VIEW v_rtm AS
SELECT r.item_id AS requirement_id, r.title,
       group_concat(DISTINCT CASE WHEN rel.rel_type = 'verified_by'  THEN rel.to_id END) AS verified_by,
       group_concat(DISTINCT CASE WHEN rel.rel_type = 'implements'   THEN rel.from_id END) AS implemented_by
FROM item r
LEFT JOIN relation rel ON (rel.from_type = 'item' AND rel.from_id = r.item_id AND rel.rel_type = 'verified_by')
                       OR (rel.to_type = 'item' AND rel.to_id = r.item_id AND rel.rel_type = 'implements')
WHERE r.item_type = 'requirement'
GROUP BY r.item_id;

-- 최신 문서 리비전 (head)
CREATE VIEW v_doc_head AS
SELECT r.* FROM doc_revision r
WHERE NOT EXISTS (SELECT 1 FROM doc_revision n WHERE n.prev_revision_id = r.revision_id);

-- ---------------------------------------------------------------------
-- AI 협업 조회: 최근 값 · 이력 · 인수인계 · 재개
-- ---------------------------------------------------------------------
-- 최근 변경 (누가·무엇을·어느 작업에서) — 사람/AI 구분
CREATE VIEW v_recent_changes AS
SELECT c.change_id, c.changed_at, c.entity_type, c.entity_id, c.seq, c.op,
       c.actor_id, a.actor_type, c.work_id, c.reason,
       json_extract(c.after_json, '$') AS after_value
FROM change_log c JOIN actor a ON a.actor_id = c.actor_id
ORDER BY c.change_id DESC;

-- 엔터티별 최신 변경 (링크드 리스트의 head)
CREATE VIEW v_entity_head AS
SELECT c.* FROM change_log c
WHERE NOT EXISTS (SELECT 1 FROM change_log n WHERE n.prev_change_id = c.change_id);

-- AI가 읽은 뒤 바뀐 근거 (낡은 컨텍스트) — 재개·인계 전에 다시 읽어야 할 목록
CREATE VIEW v_context_stale AS
SELECT r.work_id, r.actor_id, r.target_type, r.target_id, r.read_at,
       r.last_change_id AS read_change_id, max(c.change_id) AS latest_change_id
FROM context_read r
JOIN change_log c ON (c.entity_id = r.target_id OR c.entity_id LIKE r.target_id || '|%')
WHERE c.change_id > coalesce(r.last_change_id, 0)
GROUP BY r.read_id;

-- 작업 재개 카드: 상태·마지막 인계·미해결 지적·미실행/실패 실행·낡은 근거 수
CREATE VIEW v_work_resume AS
SELECT w.work_id, w.title, w.status, w.owner_id,
       (SELECT h.handoff_id FROM handoff h WHERE h.work_id = w.work_id ORDER BY h.created_at DESC LIMIT 1) AS last_handoff_id,
       (SELECT h.ack_status FROM handoff h WHERE h.work_id = w.work_id ORDER BY h.created_at DESC LIMIT 1) AS last_handoff_ack,
       (SELECT json_extract(h.payload, '$.next_actions') FROM handoff h WHERE h.work_id = w.work_id
          ORDER BY h.created_at DESC LIMIT 1) AS next_actions,
       (SELECT count(*) FROM finding f JOIN run r ON r.run_id = f.run_id
          WHERE r.work_id = w.work_id AND f.disposition = 'open') AS open_findings,
       (SELECT count(*) FROM run r WHERE r.work_id = w.work_id AND r.result IN ('FAIL','NOT_RUN','ERROR','UNKNOWN')) AS unresolved_runs,
       (SELECT count(*) FROM v_context_stale s WHERE s.work_id = w.work_id) AS stale_context,
       (SELECT max(changed_at) FROM change_log c WHERE c.work_id = w.work_id) AS last_change_at
FROM work w
WHERE w.status NOT IN ('closed','cancelled');

CREATE INDEX ix_relation_from ON relation(from_type, from_id);
CREATE INDEX ix_relation_to   ON relation(to_type, to_id);
CREATE INDEX ix_item_type     ON item(project_id, item_type);
CREATE INDEX ix_change_entity ON change_log(entity_type, entity_id, seq);
CREATE INDEX ix_run_work      ON run(work_id, result);
