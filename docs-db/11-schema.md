# 11. DB 스키마 참조

`docdb`의 마이그레이션 0001~0008을 빈 DB에 `migrate.apply_pending`으로 적용한 뒤(`PRAGMA user_version` = 8) `PRAGMA table_xinfo`, `PRAGMA foreign_key_list`, `sqlite_master`로 조회한 결과와 대조해 갱신했다(2026-10-14, T30). 컬럼·타입·NULL·인덱스는 문서와 DB를 스크립트로 대조해 일치를 확인했다. 설명 열은 SQL 주석에서 가져왔다. 마이그레이션 0001·0002는 원본 SQL을 바이트 그대로 복사한 것이라 원본 주석이 그대로 남아 있고, 0003~0008이 7절의 변경을 적용한다.

| 항목 | 값 |
|---|---|
| 원본 | `reference/docdb-package/docdb_schema.sql` (sha256 앞 16자리 `9e35773cdca5416c`), `docdb_schema_token_ext.sql` (`98011d2d803693e0`). 마이그레이션 `0001_base.sql`·`0002_token_ext.sql`의 해시와 같다 |
| 마이그레이션 | `docdb/src/docdb/schema/0001`~`0008`, `PRAGMA user_version` = 8 |
| 적재 검증 SQLite | 3.53.4(`pysqlite3` 0.6.1 내장) |
| 테이블 | 45개: 일반 테이블 40개(기본 36, 토큰 확장 3, `import_segment` 1), 가상 테이블 `fts_text` 1개, FTS5(전문 검색)가 자동으로 만드는 내부 테이블 4개(`fts_text_config` `fts_text_data` `fts_text_docsize` `fts_text_idx`) |
| 뷰 | 12개 (기본 8, 토큰 확장 3, `v_missing_required_links` 1) |
| 트리거 | 27개 (기본 12, 토큰 확장 5, 마이그레이션 추가 10) |
| 인덱스 | 명시적 인덱스 9개(`ix_*`) + `ux_single_accountable`(`role_assignment`의 UNIQUE 부분 인덱스). 그 밖에 UNIQUE·PK가 만드는 자동 인덱스 42개 |
| 변경 적용 | 7절(2026-10-13 결정 반영)은 모두 마이그레이션 0003~0008로 적용됨 |

## 1. 계층별 테이블

| 계층 | 테이블 |
|---|---|
| R 규칙(카탈로그) | `cat_version` `cat_group` `cat_fact` `cat_kind` `cat_kind_field` `cat_kind_link` `cat_kind_alias` `cat_item_type` |
| P 프로젝트 프로필 | `project` `kind_applicability` `fact_value` `kind_selection` `policy_exception` |
| I 실체 | `actor` `document` `doc_tag` `document_kind` `doc_reviewer` `field_value` `item` `role_assignment` `doc_scope` `artifact` `doc_artifact` `relation` `link_exemption` |
| W 작업·사건·증거 | `work` `work_event` `run` `finding` `approval` `handoff` `context_read` |
| H 이력 | `change_log` `doc_revision` |
| D 파생 기록 | `generation` |
| T 토큰 확장(docdb_schema_token_ext.sql) | `entity_summary` `text_unit` `fts_text` `call_log` |
| 가져오기(import) 확장(마이그레이션 0005) | `import_segment` |

`fts_text`는 FTS5(전문 검색) 가상 테이블이며, 내부 보조 테이블 `fts_text_config` `fts_text_data` `fts_text_docsize` `fts_text_idx`가 함께 만들어진다. 이 4개는 직접 읽거나 쓰지 않는다. 컬럼은 PRAGMA 조회 기준으로 `fts_text_config`(k, v), `fts_text_data`(id, block), `fts_text_docsize`(id, sz), `fts_text_idx`(segid, term, pgno)다.

## 2. 외래 키(FK) 관계

FK는 다른 테이블의 행을 가리키는 값이다. 복합 FK는 컬럼을 묶어 한 줄로 적었다. `PRAGMA foreign_keys=ON`인 연결에서는 존재하지 않는 대상을 가리키는 입력을 DB가 거부한다(SQLite는 연결마다 켜야 함).

| 테이블.컬럼 | → 참조 대상 |
|---|---|
| `cat_kind`.(group_code) | `cat_group`.(group_code) |
| `cat_kind_field`.(kind_id) | `cat_kind`.(kind_id) |
| `cat_kind_link`.(target_kind_id) | `cat_kind`.(kind_id) |
| `cat_kind_link`.(kind_id) | `cat_kind`.(kind_id) |
| `cat_kind_alias`.(kind_id) | `cat_kind`.(kind_id) |
| `cat_item_type`.(defined_by_kind) | `cat_kind`.(kind_id) |
| `project`.(catalogue_version) | `cat_version`.(catalogue_version) |
| `kind_applicability`.(kind_id) | `cat_kind`.(kind_id) |
| `kind_applicability`.(project_id) | `project`.(project_id) |
| `fact_value`.(confirmed_by) | `actor`.(actor_id) |
| `fact_value`.(fact_key) | `cat_fact`.(fact_key) |
| `fact_value`.(project_id) | `project`.(project_id) |
| `kind_selection`.(kind_id) | `cat_kind`.(kind_id) |
| `kind_selection`.(project_id) | `project`.(project_id) |
| `policy_exception`.(approved_by) | `actor`.(actor_id) |
| `policy_exception`.(kind_id) | `cat_kind`.(kind_id) |
| `policy_exception`.(project_id) | `project`.(project_id) |
| `work`.(owner_id) | `actor`.(actor_id) |
| `work`.(project_id) | `project`.(project_id) |
| `document`.(brief_parent_doc_id) | `document`.(doc_id) |
| `document`.(superseded_by) | `document`.(doc_id) |
| `document`.(origin_work_id) | `work`.(work_id) |
| `document`.(work_id) | `work`.(work_id) |
| `document`.(owner_id) | `actor`.(actor_id) |
| `document`.(category) | `cat_group`.(group_code) |
| `document`.(project_id) | `project`.(project_id) |
| `doc_tag`.(doc_id) | `document`.(doc_id) |
| `document_kind`.(kind_id) | `cat_kind`.(kind_id) |
| `document_kind`.(doc_id) | `document`.(doc_id) |
| `doc_reviewer`.(actor_id) | `actor`.(actor_id) |
| `doc_reviewer`.(doc_id) | `document`.(doc_id) |
| `field_value`.(kind_id, field_key) | `cat_kind_field`.(kind_id, field_key) |
| `field_value`.(doc_id, kind_id) | `document_kind`.(doc_id, kind_id) |
| `field_value`.(author_id) | `actor`.(actor_id) |
| `field_value`.(ref_artifact_id) | `artifact`.(artifact_id) |
| `item`.(defined_in) | `document`.(doc_id) |
| `item`.(item_type) | `cat_item_type`.(item_type) |
| `item`.(project_id) | `project`.(project_id) |
| `role_assignment`.(actor_id) | `actor`.(actor_id) |
| `role_assignment`.(role_item_id) | `item`.(item_id) |
| `doc_scope`.(item_id) | `item`.(item_id) |
| `doc_scope`.(doc_id) | `document`.(doc_id) |
| `doc_artifact`.(artifact_id) | `artifact`.(artifact_id) |
| `doc_artifact`.(doc_id) | `document`.(doc_id) |
| `relation`.(target_revision_id) | `doc_revision`.(revision_id) |
| `relation`.(proposed_by) | `actor`.(actor_id) |
| `relation`.(decided_by) | `actor`.(actor_id) |
| `link_exemption`.(target_kind_id) | `cat_kind`.(kind_id) |
| `link_exemption`.(doc_id) | `document`.(doc_id) |
| `import_segment`.(artifact_id) | `artifact`.(artifact_id) |
| `import_segment`.(doc_id) | `document`.(doc_id) |
| `import_segment`.(kind_id, field_key) | `cat_kind_field`.(kind_id, field_key) |
| `work_event`.(declared_by) | `actor`.(actor_id) |
| `work_event`.(fact_key) | `cat_fact`.(fact_key) |
| `work_event`.(work_id) | `work`.(work_id) |
| `run`.(raw_artifact_id) | `artifact`.(artifact_id) |
| `run`.(env_item_id) | `item`.(item_id) |
| `run`.(work_id) | `work`.(work_id) |
| `finding`.(run_id) | `run`.(run_id) |
| `approval`.(approver_id) | `actor`.(actor_id) |
| `approval`.(work_id) | `work`.(work_id) |
| `approval`.(revision_id) | `doc_revision`.(revision_id) |
| `handoff`.(ack_by) | `actor`.(actor_id) |
| `handoff`.(work_id) | `work`.(work_id) |
| `context_read`.(last_change_id) | `change_log`.(change_id) |
| `context_read`.(revision_id) | `doc_revision`.(revision_id) |
| `context_read`.(actor_id) | `actor`.(actor_id) |
| `context_read`.(work_id) | `work`.(work_id) |
| `change_log`.(work_id) | `work`.(work_id) |
| `change_log`.(actor_id) | `actor`.(actor_id) |
| `change_log`.(prev_change_id) | `change_log`.(change_id) |
| `doc_revision`.(created_by) | `actor`.(actor_id) |
| `doc_revision`.(last_change_id) | `change_log`.(change_id) |
| `doc_revision`.(prev_revision_id) | `doc_revision`.(revision_id) |
| `doc_revision`.(doc_id) | `document`.(doc_id) |
| `generation`.(source_revision_id) | `doc_revision`.(revision_id) |
| `entity_summary`.(author_id) | `actor`.(actor_id) |
| `call_log`.(work_id) | `work`.(work_id) |
| `call_log`.(actor_id) | `actor`.(actor_id) |

## 3. 테이블 상세

설명 문구는 0001·0002(원본 SQL)의 주석을 그대로 옮겼으므로 원 카탈로그 194종 기준 수치(`cat_kind` 194개, `cat_kind_field` 필수 897 등)가 남아 있다. 시작 카탈로그는 18종(D2)이라 이 수치와 일치하지 않는다. 0003 이후에 추가된 컬럼·테이블은 설명 열에 마이그레이션 번호를 적었다.

키·제약 열: `PK` 기본 키, `→` FK, 그 외 SQL에 적힌 제약(CHECK·UNIQUE·DEFAULT 등)을 그대로 옮겼다.

### R 규칙(카탈로그)

#### `cat_version`

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `catalogue_version` | TEXT | N | PK | standard_version 예: 1.0.0-draft.1 |
| `schema_version` | TEXT | N |  |  |
| `source_digest` | TEXT | N |  | catalogue.yaml sha256 |
| `loaded_at` | TEXT | N |  |  |

#### `cat_group`

A~Z 26개 영역

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `group_code` | TEXT | N | PK CHECK (length(group_code) = 1) |  |
| `directory` | TEXT | N | UNIQUE |  |
| `title_ko` | TEXT | N |  |  |
| `title_en` | TEXT | N |  |  |
| `default_owner_role` | TEXT | N |  |  |
| `review_days` | INTEGER | Y |  |  |

#### `cat_fact`

cap.* 38개 / evt.* 53개

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `fact_key` | TEXT | N | PK | 'cap.api', 'evt.release' |
| `fact_type` | TEXT | N | CHECK (fact_type IN ('cap','evt')) |  |
| `description` | TEXT | N |  |  |
| `derived_rule` | TEXT | Y | CHECK (derived_rule IS NULL OR json_valid(derived_rule)) | cap.integration = OR(...) |

#### `cat_kind`

문서 종류 194개

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `kind_id` | TEXT | N | PK | 'D01' |
| `abbreviation` | TEXT | N | UNIQUE | 'REQ-SPEC' |
| `title_ko` | TEXT | N |  |  |
| `title_en` | TEXT | N |  |  |
| `group_code` | TEXT | N | → cat_group.group_code |  |
| `lifecycle` | TEXT | N | CHECK (lifecycle IN ('living','work')) |  |
| `mode` | TEXT | N | CHECK (mode IN ('mandatory','conditional','optional')) |  |
| `storage_class` | TEXT | N | DEFAULT 'narrative' |  |
| `owner_role` | TEXT | N |  |  |
| `review_days` | INTEGER | Y |  |  |
| `trigger_text` | TEXT | Y |  |  |
| `required_when` | TEXT | N | CHECK (json_valid(required_when)) | 제한 연산만: always/fact/all/any/selected_kind |
| `applies_when` | TEXT | N | CHECK (json_valid(applies_when)) |  |
| `content_files` | TEXT | N | CHECK (json_valid(content_files)) | 생성 시 출력 파일 구성 |

테이블 제약:

- `CHECK (storage_class IN ('narrative','register','view','run'))` — narrative: 서술 필드(field_value) / register: 항목 대장(item 행) view: 쿼리로 생성(작성 안 함) / run: 실행 기록(run 행)

#### `cat_kind_field`

필수 897 / 선택 387

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `kind_id` | TEXT | N | PK → cat_kind.kind_id |  |
| `field_key` | TEXT | N | PK |  |
| `is_required` | INTEGER | N | CHECK (is_required IN (0,1)) |  |
| `description` | TEXT | N |  |  |
| `brief_preserve` | INTEGER | N | DEFAULT 0 CHECK (brief_preserve IN (0,1)) |  |
| `sort_order` | INTEGER | N |  | 생성 문서의 절 순서 |
| `extract_rel` | TEXT | Y | CHECK (extract_rel IS NULL OR json_valid(extract_rel)) | 0006 추가. 7.5절 |

테이블 제약:

- `PRIMARY KEY (kind_id, field_key)`

#### `cat_kind_link`

필수 194 / 선택 575 (종류 간 연결 규칙)

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `kind_id` | TEXT | N | PK → cat_kind.kind_id |  |
| `target_kind_id` | TEXT | N | PK → cat_kind.kind_id |  |
| `is_required` | INTEGER | N | CHECK (is_required IN (0,1)) |  |

테이블 제약:

- `PRIMARY KEY (kind_id, target_kind_id)`

#### `cat_kind_alias`

covered_names 768개: 검색·이관 매핑용

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `kind_id` | TEXT | N | PK → cat_kind.kind_id |  |
| `name` | TEXT | N | PK |  |
| `is_primary` | INTEGER | N | DEFAULT 1 CHECK (is_primary IN (0,1)) | 같은 이름이 여러 종류에 있을 때 대표 1개 |

테이블 제약:

- `PRIMARY KEY (kind_id, name)`

#### `cat_item_type`

[확장 제안] 카탈로그에 없음. 고정 ID 항목의 종류·접두어 규칙

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `item_type` | TEXT | N | PK | 'requirement' |
| `id_prefix` | TEXT | N | UNIQUE | 'REQ-' |
| `defined_by_kind` | TEXT | N | → cat_kind.kind_id | 이 항목의 대장(정의 문서) 종류 |
| `description` | TEXT | N |  |  |
| `defined_by_field` | TEXT | Y |  | 0006 추가. 7.9절 |

### P 프로젝트 프로필

#### `project`

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `project_id` | TEXT | N | PK | 'PRJ-xxx' |
| `name` | TEXT | N |  |  |
| `catalogue_version` | TEXT | N | → cat_version.catalogue_version |  |
| `profile_status` | TEXT | N | DEFAULT 'draft' CHECK (profile_status IN ('draft','approved')) |  |
| `profile_revision` | INTEGER | N | DEFAULT 1 | 사실·선택이 바뀌어 승인될 때 +1 (applicability 근거 버전) |
| `retention_policy_ref` | TEXT | Y |  |  |

#### `kind_applicability`

[파생 캐시] 규칙 평가 결과. 스크립트가 재계산

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `project_id` | TEXT | N | PK → project.project_id |  |
| `kind_id` | TEXT | N | PK → cat_kind.kind_id |  |
| `applies` | TEXT | N | CHECK (applies IN ('true','false','unknown')) |  |
| `required` | TEXT | N | CHECK (required IN ('true','false','unknown')) |  |
| `reason` | TEXT | N |  | 평가에 쓰인 사실 목록 |
| `profile_revision` | INTEGER | N |  |  |
| `evaluated_at` | TEXT | N |  |  |

테이블 제약:

- `PRIMARY KEY (project_id, kind_id)`

#### `fact_value`

세 값 논리: 1 참 / 0 거짓 / NULL 미확인

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `project_id` | TEXT | N | PK → project.project_id |  |
| `fact_key` | TEXT | N | PK → cat_fact.fact_key |  |
| `value` | INTEGER | Y | CHECK (value IS NULL OR value IN (0,1)) |  |
| `reason` | TEXT | Y |  |  |
| `evidence_ref` | TEXT | Y |  |  |
| `confirmed_by` | TEXT | Y | → actor.actor_id |  |
| `confirmed_at` | TEXT | Y |  |  |

테이블 제약:

- `PRIMARY KEY (project_id, fact_key)`
- `CHECK (fact_key LIKE 'cap.%')` — evt.* 는 작업 단위(work_event)에서 관리

#### `kind_selection`

selected_kinds / promoted_kinds

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `project_id` | TEXT | N | PK → project.project_id |  |
| `kind_id` | TEXT | N | PK → cat_kind.kind_id |  |
| `selection` | TEXT | N | CHECK (selection IN ('selected','promoted')) |  |
| `reason` | TEXT | N |  |  |

테이블 제약:

- `PRIMARY KEY (project_id, kind_id)`

#### `policy_exception`

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `exception_id` | TEXT | N | PK |  |
| `project_id` | TEXT | N | → project.project_id |  |
| `kind_id` | TEXT | Y | → cat_kind.kind_id |  |
| `scope` | TEXT | N |  |  |
| `reason` | TEXT | N |  |  |
| `approved_by` | TEXT | N | → actor.actor_id |  |
| `expires_at` | TEXT | N |  |  |

### I 실체

#### `actor`

사람·팀·AI 모델. AI는 최종 책임자/승인자 불가

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `actor_id` | TEXT | N | PK | 'team-backend', 'u-jjm', 'ai-codex-x' |
| `actor_type` | TEXT | N | CHECK (actor_type IN ('human','team','ai')) |  |
| `name` | TEXT | N |  |  |
| `active` | INTEGER | N | DEFAULT 1 |  |

#### `document`

논리 문서 묶음 (meta.yaml 대체)

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `doc_id` | TEXT | N | PK | 'DOC-REQ-ORDERS' 불변, 재사용 금지 |
| `project_id` | TEXT | N | → project.project_id |  |
| `title` | TEXT | N |  |  |
| `language` | TEXT | N | DEFAULT 'ko' |  |
| `lifecycle` | TEXT | N | CHECK (lifecycle IN ('living','work')) |  |
| `category` | TEXT | N | → cat_group.group_code |  |
| `status` | TEXT | N | DEFAULT 'draft' |  |
| `authority` | TEXT | N | CHECK (authority IN ('normative','evidence','derived')) |  |
| `baseline_mode` | TEXT | N | CHECK (baseline_mode IN ('desired','observed','historical')) |  |
| `source_mode` | TEXT | N | DEFAULT 'authored' |  |
| `owner_id` | TEXT | N | → actor.actor_id |  |
| `classification` | TEXT | N | DEFAULT 'internal' |  |
| `export_policy` | TEXT | N | DEFAULT 'local_only' |  |
| `work_id` | TEXT | Y | → work.work_id |  |
| `origin_work_id` | TEXT | Y | → work.work_id | living 문서를 만든/바꾼 작업 |
| `superseded_by` | TEXT | Y | → document.doc_id |  |
| `archive_reason` | TEXT | Y |  |  |
| `legal_hold` | INTEGER | N | DEFAULT 0 CHECK (legal_hold IN (0,1)) |  |
| `retention_policy_ref` | TEXT | Y |  | review (5.1) |
| `last_reviewed_at` | TEXT | Y |  | 실제 의미 검토한 경우에만 갱신 |
| `review_interval_days` | INTEGER | Y |  | NULL이면 cat_kind.review_days 사용 (override만 저장) |
| `review_triggers` | TEXT | N | DEFAULT '[]' CHECK (json_valid(review_triggers)) | applicability (5.1) |
| `applicability_status` | TEXT | N | DEFAULT 'unknown' |  |
| `applicability_reason` | TEXT | Y |  |  |
| `applicability_profile_rev` | INTEGER | Y |  | brief exemption (5.2) |
| `brief_exemption_reason` | TEXT | Y |  |  |
| `brief_parent_doc_id` | TEXT | Y | → document.doc_id |  |
| `extensions` | TEXT | N | DEFAULT '{}' CHECK (json_valid(extensions)) |  |
| `created_at` | TEXT | N |  |  |
| `updated_at` | TEXT | N |  |  |

테이블 제약:

- `CHECK (status IN ('draft','in_review','approved','superseded','archived'))`
- `CHECK (source_mode IN ('authored','generated','external_reference'))`
- `CHECK (classification IN ('public','internal','confidential','restricted'))`
- `CHECK (export_policy IN ('public','approved_providers','local_only'))`
- `CHECK (applicability_status IN ('applicable','not_applicable','unknown'))`
- `CHECK ((lifecycle = 'work') = (work_id IS NOT NULL))` — work 문서는 작업에 반드시 소속
- `CHECK (status <> 'archived' OR archive_reason IS NOT NULL)`
- `CHECK (status <> 'superseded' OR superseded_by IS NOT NULL)`
- `CHECK (brief_exemption_reason IS NULL OR brief_parent_doc_id IS NOT NULL)`

인덱스:

- `ix_doc_work`: `CREATE INDEX ix_doc_work ON document(work_id)`
- `ix_doc_origin`: `CREATE INDEX ix_doc_origin ON document(origin_work_id)`

트리거: `document_legal_hold`, `document_id_format`

#### `doc_tag`

tags / aliases (5.2 선택)

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `doc_id` | TEXT | N | PK → document.doc_id |  |
| `tag` | TEXT | N | PK |  |
| `tag_type` | TEXT | N | PK DEFAULT 'tag' CHECK (tag_type IN ('tag','alias')) |  |

테이블 제약:

- `PRIMARY KEY (doc_id, tag, tag_type)`

#### `document_kind`

kind_ids (통합 문서는 여러 개)

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `doc_id` | TEXT | N | PK → document.doc_id |  |
| `kind_id` | TEXT | N | PK → cat_kind.kind_id |  |

테이블 제약:

- `PRIMARY KEY (doc_id, kind_id)`

트리거: `document_kind_lifecycle`

#### `doc_reviewer`

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `doc_id` | TEXT | N | PK → document.doc_id |  |
| `actor_id` | TEXT | N | PK → actor.actor_id |  |

테이블 제약:

- `PRIMARY KEY (doc_id, actor_id)`

#### `field_value`

본문 = 필드 단위 데이터 (detail.md + field_bindings 대체)

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `doc_id` | TEXT | N | PK → document_kind.doc_id |  |
| `kind_id` | TEXT | N | PK → document_kind.kind_id |  |
| `field_key` | TEXT | N | PK → cat_kind_field.field_key |  |
| `fill_state` | TEXT | N | CHECK (fill_state IN ('filled','tbd','unknown','not_applicable')) |  |
| `format` | TEXT | N | DEFAULT 'md' CHECK (format IN ('md','json','ref')) |  |
| `value_text` | TEXT | Y |  | format=md |
| `value_json` | TEXT | Y | CHECK (value_json IS NULL OR json_valid(value_json)) | format=json |
| `ref_artifact_id` | TEXT | Y | → artifact.artifact_id | format=ref (Git 원본의 절/포인터) |
| `ref_locator` | TEXT | Y |  | '#behavior' 또는 '/criteria' |
| `reason` | TEXT | Y |  | tbd/unknown/not_applicable 사유 필수 |
| `author_id` | TEXT | N | → actor.actor_id |  |
| `updated_at` | TEXT | N |  |  |
| `digest` | TEXT | Y |  | sha256 앞 16자리면 충분 (프로젝트 내 비교용) (토큰 확장으로 추가) |
| `tok_est` | INTEGER | Y |  | 본문 토큰 추정치 (보정 전 휴리스틱) (토큰 확장으로 추가) |

테이블 제약:

- `PRIMARY KEY (doc_id, kind_id, field_key)`
- `FOREIGN KEY (doc_id, kind_id) REFERENCES document_kind(doc_id, kind_id)`
- `FOREIGN KEY (kind_id, field_key) REFERENCES cat_kind_field(kind_id, field_key)`
- `CHECK (fill_state = 'filled' OR reason IS NOT NULL)`
- `CHECK (format <> 'ref' OR ref_artifact_id IS NOT NULL)`

트리거: `fv_fts_ai`, `fv_fts_au`, `fv_fts_ad`

#### `item`

고정 ID 항목: REQ/AC/SCR/API/TEST/ERR/RISK/TERM/SVC/FEAT/ENV/HW/...

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `item_id` | TEXT | N | PK | 'REQ-ORD-001', 'HW-DB01', 'ENV-PROD' |
| `project_id` | TEXT | N | → project.project_id |  |
| `item_type` | TEXT | N | → cat_item_type.item_type |  |
| `title` | TEXT | N |  |  |
| `status` | TEXT | Y |  |  |
| `defined_in` | TEXT | Y | → document.doc_id | 이 항목을 정의하는 문서(대장) |
| `attrs` | TEXT | N | DEFAULT '{}' CHECK (json_valid(attrs)) | 기대(desired) 속성. 관측값은 run/finding |
| `created_at` | TEXT | N |  |  |
| `updated_at` | TEXT | N |  |  |
| `digest` | TEXT | Y |  |  |
| `tok_est` | INTEGER | Y |  |  |

인덱스:

- `ix_item_type`: `CREATE INDEX ix_item_type ON item(project_id, item_type)`
- `ix_item_defined`: `CREATE INDEX ix_item_defined ON item(defined_in)`

트리거: `item_id_prefix`, `item_fts_ai`, `item_fts_au`, `item_fts_ad`, `item_id_format`

#### `role_assignment`

B03 RACI: 역할(item) ↔ 실제 담당(actor)

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `role_item_id` | TEXT | N | PK → item.item_id |  |
| `actor_id` | TEXT | N | PK → actor.actor_id |  |
| `raci` | TEXT | N | PK CHECK (raci IN ('R','A','C','I')) |  |
| `valid_from` | TEXT | N | PK |  |
| `valid_to` | TEXT | Y |  |  |

테이블 제약:

- `PRIMARY KEY (role_item_id, actor_id, raci, valid_from)`

인덱스:

- `ux_single_accountable`: `CREATE UNIQUE INDEX ux_single_accountable ON role_assignment(role_item_id) WHERE raci = 'A' AND valid_to IS NULL`

#### `doc_scope`

scope: 서비스·기능·환경·자산

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `doc_id` | TEXT | N | PK → document.doc_id |  |
| `item_id` | TEXT | N | PK → item.item_id |  |

테이블 제약:

- `PRIMARY KEY (doc_id, item_id)`

#### `artifact`

Git 파일·외부 객체·원시 증거 (내용은 복제하지 않음)

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `artifact_id` | TEXT | N | PK |  |
| `uri` | TEXT | N |  | 저장소 상대 POSIX 경로 또는 외부 URI |
| `repo` | TEXT | Y |  |  |
| `commit_sha` | TEXT | Y |  |  |
| `digest` | TEXT | Y |  | sha256:... |
| `role` | TEXT | N | CHECK (role IN ('primary','structured','contract','migration','iac','code', 'design','evidence','dataset','derived')) |  |
| `source_mode` | TEXT | N | CHECK (source_mode IN ('authored','generated','external_reference')) |  |
| `external_version` | TEXT | Y |  |  |
| `access_policy_ref` | TEXT | Y |  |  |
| `retention_policy_ref` | TEXT | Y |  |  |

테이블 제약:

- `CHECK (uri LIKE '%://%' OR (uri NOT LIKE '/%' AND uri NOT LIKE '%..%' AND uri NOT LIKE '%\%' AND uri NOT LIKE '_:%'))` — 저장소 경로는 상대 POSIX만: 절대경로·../·역슬래시·드라이브 접두사 거부 (외부 URI는 scheme 필수)

트리거: `artifact_id_format`

#### `doc_artifact`

files (5.1): 문서 ↔ 원본·구조·계약·증거 파일

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `doc_id` | TEXT | N | PK → document.doc_id |  |
| `artifact_id` | TEXT | N | PK → artifact.artifact_id |  |
| `file_role` | TEXT | N |  | primary/structured/contract/evidence/derived |

테이블 제약:

- `PRIMARY KEY (doc_id, artifact_id)`

#### `relation`

모든 연결은 여기 한 곳 (RTM·의존성 맵은 이 테이블의 쿼리)

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `rel_id` | INTEGER | N | PK |  |
| `from_type` | TEXT | N | CHECK (from_type IN ('document','item','artifact','work','run')) |  |
| `from_id` | TEXT | N |  |  |
| `rel_type` | TEXT | N | CHECK (rel_type IN ('references','implements','verified_by','constrained_by', 'depends_on','uses_data','visualizes','generated_from','changes','supersedes', 'evidence_for','handoff_to')) |  |
| `to_type` | TEXT | N | CHECK (to_type IN ('document','item','artifact','work','run')) |  |
| `to_id` | TEXT | N |  |  |
| `is_required` | INTEGER | N | DEFAULT 0 CHECK (is_required IN (0,1)) |  |
| `target_revision_id` | INTEGER | Y | → doc_revision.revision_id |  |
| `anchor` | TEXT | Y |  |  |
| `reason` | TEXT | Y |  |  |
| `created_at` | TEXT | N |  |  |
| `status` | TEXT | N | DEFAULT 'confirmed' CHECK (status IN ('proposed','confirmed','rejected')) | 0003 추가. 7.1절 |
| `source` | TEXT | N | DEFAULT 'human' | 0003 추가. `rule:<규칙 ID>` / `agent` / `human` |
| `proposed_by` | TEXT | Y | → actor.actor_id | 0003 추가 |
| `decided_by` | TEXT | Y | → actor.actor_id | 0003 추가 |
| `decided_at` | TEXT | Y |  | 0003 추가 |

테이블 제약:

- `UNIQUE (from_type, from_id, rel_type, to_type, to_id)`

인덱스:

- `ix_relation_from`: `CREATE INDEX ix_relation_from ON relation(from_type, from_id)`
- `ix_relation_to`: `CREATE INDEX ix_relation_to ON relation(to_type, to_id)`

트리거: `relation_source_status`, `relation_decide_human_only`

#### `link_exemption`

필수 연결 대상이 비적용/미확인일 때 N/A + 이유 + 프로필 근거 (6.1)

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `doc_id` | TEXT | N | PK → document.doc_id |  |
| `target_kind_id` | TEXT | N | PK → cat_kind.kind_id |  |
| `status` | TEXT | N | CHECK (status IN ('not_applicable','unknown')) |  |
| `reason` | TEXT | N |  |  |
| `profile_revision` | INTEGER | N |  |  |

테이블 제약:

- `PRIMARY KEY (doc_id, target_kind_id)`

### W 작업·사건·증거

#### `work`

WORK-YYYY-NNNN

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `work_id` | TEXT | N | PK CHECK (work_id GLOB 'WORK-[0-9][0-9][0-9][0-9]-[0-9]*') |  |
| `project_id` | TEXT | N | → project.project_id |  |
| `work_type` | TEXT | N |  | feature/bugfix/release/incident/migration/audit/... |
| `title` | TEXT | N |  |  |
| `status` | TEXT | N | DEFAULT 'planned' |  |
| `owner_id` | TEXT | N | → actor.actor_id |  |
| `base_refs` | TEXT | N | DEFAULT '[]' CHECK (json_valid(base_refs)) | 기준 커밋·doc_revision |
| `result_refs` | TEXT | N | DEFAULT '[]' CHECK (json_valid(result_refs)) | 결과 커밋·산출물 |
| `opened_at` | TEXT | N |  |  |
| `closed_at` | TEXT | Y |  |  |

테이블 제약:

- `CHECK (status IN ('planned','active','blocked','review','verified','closed','cancelled'))`

트리거: `work_id_format`

#### `work_event`

evt.* 사실: 작업마다 선언

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `work_id` | TEXT | N | PK → work.work_id |  |
| `fact_key` | TEXT | N | PK → cat_fact.fact_key |  |
| `value` | INTEGER | Y | CHECK (value IS NULL OR value IN (0,1)) |  |
| `declared_by` | TEXT | N | → actor.actor_id |  |
| `source` | TEXT | N | CHECK (source IN ('declared','git_diff','ci','script')) |  |

테이블 제약:

- `PRIMARY KEY (work_id, fact_key)`
- `CHECK (fact_key LIKE 'evt.%')`

#### `run`

테스트·빌드·배포·백업·복원·훈련·스캔 실행 증거 (9.4)

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `run_id` | TEXT | N | PK |  |
| `work_id` | TEXT | Y | → work.work_id |  |
| `run_type` | TEXT | N |  | test/build/deploy/backup/restore/dr_drill/scan/hw_check/... |
| `target_ref` | TEXT | N |  | commit 또는 artifact digest |
| `target_versions` | TEXT | N | DEFAULT '{}' CHECK (json_valid(target_versions)) | 데이터·스키마·프롬프트 버전 |
| `env_item_id` | TEXT | Y | → item.item_id | 환경·하드웨어 자산 |
| `env_snapshot` | TEXT | N | DEFAULT '{}' CHECK (json_valid(env_snapshot)) | 런타임·의존 버전·설정(민감값 제외) |
| `runner` | TEXT | N |  |  |
| `command_argv` | TEXT | Y | CHECK (command_argv IS NULL OR json_valid(command_argv)) | 셸 문자열 금지, 배열 |
| `started_at` | TEXT | Y |  |  |
| `ended_at` | TEXT | Y |  |  |
| `exit_code` | INTEGER | Y |  |  |
| `result` | TEXT | N | CHECK (result IN ('PASS','FAIL','NOT_RUN','ERROR','UNKNOWN')) |  |
| `raw_artifact_id` | TEXT | Y | → artifact.artifact_id |  |
| `limitations` | TEXT | Y |  | Mock·샘플링·flaky·환경 차이 |
| `trust_source` | TEXT | N | CHECK (trust_source IN ('ci','runner','manual','ai_reported')) |  |
| `attestation_ref` | TEXT | Y |  | 서명·provenance 참조 |
| `recorded_at` | TEXT | N |  |  |

인덱스:

- `ix_run_work`: `CREATE INDEX ix_run_work ON run(work_id, result)`

트리거: `run_no_update`, `run_id_format`

#### `finding`

검사·리뷰·보안·드리프트 결과의 개별 지적 (A06/G07/M08/K07/S04/...)

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `finding_id` | INTEGER | N | PK |  |
| `run_id` | TEXT | N | → run.run_id |  |
| `code` | TEXT | N |  | DOC_MISSING/FIELD_MISSING/BRIEF_STALE/DRIFT/VULN/... |
| `severity` | TEXT | N | CHECK (severity IN ('info','warning','error','critical')) |  |
| `target_type` | TEXT | N | CHECK (target_type IN ('document','item','artifact','work','field','project')) |  |
| `target_id` | TEXT | N |  |  |
| `message` | TEXT | N |  |  |
| `evidence` | TEXT | Y |  |  |
| `disposition` | TEXT | N | DEFAULT 'open' |  |
| `disposition_ref` | TEXT | Y |  | 조치 작업·예외 승인 ID |

테이블 제약:

- `CHECK (disposition IN ('open','fixed','accepted_exception','false_positive','deferred'))`

#### `approval`

승인은 사람만, 특정 리비전에 대해

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `approval_id` | TEXT | N | PK |  |
| `revision_id` | INTEGER | Y | → doc_revision.revision_id |  |
| `work_id` | TEXT | Y | → work.work_id |  |
| `gate` | TEXT | Y |  | design-approval/pre-merge/pre-release/work-close/... |
| `approver_id` | TEXT | N | → actor.actor_id |  |
| `decision` | TEXT | N | CHECK (decision IN ('approved','rejected')) |  |
| `scope_note` | TEXT | Y |  |  |
| `external_ref` | TEXT | Y |  | PR 리뷰·결재 시스템 ID |
| `decided_at` | TEXT | N |  |  |

테이블 제약:

- `CHECK (revision_id IS NOT NULL OR work_id IS NOT NULL)`

트리거: `approval_human_only`, `approval_id_format`

#### `handoff`

9.3 인수인계

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `handoff_id` | TEXT | N | PK |  |
| `work_id` | TEXT | N | → work.work_id |  |
| `from_role` | TEXT | N |  |  |
| `to_role` | TEXT | N |  |  |
| `payload` | TEXT | N | CHECK (json_valid(payload)) | 완료/미완료/위험/다음 행동/not_run |
| `ack_status` | TEXT | N | DEFAULT 'pending' CHECK (ack_status IN ('pending','accepted','rejected')) |  |
| `ack_by` | TEXT | Y | → actor.actor_id |  |
| `ack_at` | TEXT | Y |  |  |
| `ack_checked_baseline` | TEXT | Y |  | 수신자가 직접 확인한 기준 커밋 |
| `ack_checked_evidence` | TEXT | N | DEFAULT '[]' CHECK (json_valid(ack_checked_evidence)) |  |
| `created_at` | TEXT | N |  |  |

테이블 제약:

- `CHECK (ack_status = 'pending' OR (ack_by IS NOT NULL AND ack_checked_baseline IS NOT NULL))`

트리거: `handoff_id_format`

#### `context_read`

V05 컨텍스트 잠금: AI가 작업 중 '무엇의 어느 시점 값'을 읽었는지 자동 기록

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `read_id` | INTEGER | N | PK |  |
| `work_id` | TEXT | N | → work.work_id |  |
| `actor_id` | TEXT | N | → actor.actor_id |  |
| `session_ref` | TEXT | Y |  | 에이전트 세션·실행 ID |
| `target_type` | TEXT | N | CHECK (target_type IN ('document','item','artifact')) |  |
| `target_id` | TEXT | N |  |  |
| `revision_id` | INTEGER | Y | → doc_revision.revision_id |  |
| `last_change_id` | INTEGER | Y | → change_log.change_id | 읽은 시점의 최신 변경 (이후 변경 = 낡은 근거) |
| `digest` | TEXT | Y |  |  |
| `is_required` | INTEGER | N | DEFAULT 1 CHECK (is_required IN (0,1)) |  |
| `reason` | TEXT | Y |  | 선정 이유 |
| `read_at` | TEXT | N |  |  |

트리거: `context_read_no_update`

### H 이력

#### `change_log`

모든 엔터티의 세부 변경 (누가·언제·무엇을)

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `change_id` | INTEGER | N | PK |  |
| `entity_type` | TEXT | N |  | 'document','field_value','item','relation','fact_value',... |
| `entity_id` | TEXT | N |  | 복합키는 정규화 문자열 'DOC-X\|D01\|behavior' |
| `seq` | INTEGER | N | CHECK (seq >= 1) |  |
| `prev_change_id` | INTEGER | Y | → change_log.change_id UNIQUE | 링크드 리스트, UNIQUE=분기(fork) 금지 |
| `op` | TEXT | N | CHECK (op IN ('insert','update','delete','correct')) |  |
| `before_json` | TEXT | Y | CHECK (before_json IS NULL OR json_valid(before_json)) |  |
| `after_json` | TEXT | Y | CHECK (after_json IS NULL OR json_valid(after_json)) |  |
| `actor_id` | TEXT | N | → actor.actor_id |  |
| `work_id` | TEXT | Y | → work.work_id |  |
| `reason` | TEXT | Y |  |  |
| `changed_at` | TEXT | N |  |  |
| `row_digest` | TEXT | N |  | sha256(canonical json of this row + prev_digest) |
| `prev_digest` | TEXT | Y |  | 직전 행 digest → 위변조 탐지 |

테이블 제약:

- `UNIQUE (entity_type, entity_id, seq)` — 순회 없이 N번째 직접 조회
- `CHECK ((seq = 1) = (prev_change_id IS NULL))`

인덱스:

- `ix_change_entity`: `CREATE INDEX ix_change_entity ON change_log(entity_type, entity_id, seq)`
- `ix_change_work`: `CREATE INDEX ix_change_work ON change_log(work_id, change_id)`

트리거: `change_log_no_update`, `change_log_no_delete`, `change_log_chain_check`

#### `doc_revision`

문서 기준선(승인·배포 단위) 스냅샷

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `revision_id` | INTEGER | N | PK |  |
| `doc_id` | TEXT | N | → document.doc_id |  |
| `revision_no` | INTEGER | N | CHECK (revision_no >= 1) |  |
| `prev_revision_id` | INTEGER | Y | → doc_revision.revision_id UNIQUE |  |
| `snapshot_json` | TEXT | N | CHECK (json_valid(snapshot_json)) | 메타+필드+관계 고정 사본 |
| `content_digest` | TEXT | N |  | 5.4 규칙의 source_digest |
| `prev_digest` | TEXT | Y |  |  |
| `last_change_id` | INTEGER | Y | → change_log.change_id | 어느 변경까지 반영했는지 |
| `reason` | TEXT | N |  |  |
| `created_by` | TEXT | N | → actor.actor_id |  |
| `created_at` | TEXT | N |  |  |

테이블 제약:

- `UNIQUE (doc_id, revision_no)`
- `CHECK ((revision_no = 1) = (prev_revision_id IS NULL))`

트리거: `doc_revision_no_update`, `doc_revision_no_delete`, `doc_revision_chain_check`

### D 파생 기록

#### `generation`

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `gen_id` | INTEGER | N | PK |  |
| `output_type` | TEXT | N |  | 'doc_md','brief_en','register','rtm','status','context' |
| `target_id` | TEXT | N |  | doc_id / work_id / 'project' |
| `source_revision_id` | INTEGER | Y | → doc_revision.revision_id |  |
| `source_digest` | TEXT | N |  |  |
| `generator` | TEXT | N |  |  |
| `generator_version` | TEXT | N |  |  |
| `input_digests` | TEXT | N | DEFAULT '[]' CHECK (json_valid(input_digests)) | generation.inputs |
| `author_type` | TEXT | N | CHECK (author_type IN ('script','llm')) | LLM 생성물은 검토 필요 |
| `output_uri` | TEXT | N |  |  |
| `output_digest` | TEXT | N |  |  |
| `freshness` | TEXT | N | CHECK (freshness IN ('FRESH','STALE','UNVERIFIED','BLOCKED')) |  |
| `generated_at` | TEXT | N |  |  |

### T 토큰 확장(docdb_schema_token_ext.sql)

#### `entity_summary`

2) L1 요약 — 원본이 아닌 파생물. 원본 digest가 바뀌면 낡음

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `handle` | TEXT | N | PK | 'f:DOC-X/D01/behavior', 'i:REQ-1', 'd:DOC-X' |
| `summary` | TEXT | N |  | 1~3문장. 사실·숫자·ID 보존 |
| `source_digest` | TEXT | N |  | 요약 당시 원본 digest |
| `tok_est` | INTEGER | N |  |  |
| `author_type` | TEXT | N | CHECK (author_type IN ('script','llm')) |  |
| `author_id` | TEXT | N | → actor.actor_id |  |
| `created_at` | TEXT | N |  |  |

#### `text_unit`

3) 전문 검색 — 검색 단위 표 + external content FTS5 필드값의 암묵 rowid는 VACUUM으로 바뀔 수 있으므로(sqlite lang_vacuum §3) 명시적 INTEGER PRIMARY KEY를 가진 text_unit을 rowid로 쓴다.

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `unit_id` | INTEGER | N | PK |  |
| `src` | TEXT | N | CHECK (src IN ('f','i')) |  |
| `doc_id` | TEXT | Y | , kind_id TEXT, field_key TEXT | src='f' |
| `kind_id` | TEXT | Y |  |  |
| `field_key` | TEXT | Y |  |  |
| `item_id` | TEXT | Y |  | src='i' |

테이블 제약:

- `UNIQUE (doc_id, kind_id, field_key)`
- `UNIQUE (item_id)`

#### `fts_text` (가상 테이블)

trigram: 부분 문자열 검색(한국어 조사 붙은 단어 대응). 3글자 미만 검색어는 MATCH 불가 → 스크립트가 LIKE로 대체

```sql
CREATE VIRTUAL TABLE fts_text USING fts5(body, content='v_text_unit', content_rowid='unit_id', tokenize='trigram');
```

#### `call_log`

4) 호출 로그 — 토큰을 어디에 썼는지 측정 (context_read는 '무엇을 읽었나', call_log는 '얼마나 실었나')

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `call_id` | INTEGER | N | PK |  |
| `tool` | TEXT | N |  | ctx/get/find/trace/since/put |
| `args_json` | TEXT | N | CHECK (json_valid(args_json)) |  |
| `actor_id` | TEXT | Y | → actor.actor_id |  |
| `work_id` | TEXT | Y | → work.work_id |  |
| `out_chars` | INTEGER | N |  |  |
| `out_tok_est` | INTEGER | N |  |  |
| `budget` | INTEGER | Y |  |  |
| `omitted` | INTEGER | N | DEFAULT 0 | 예산 때문에 생략한 항목 수 |
| `called_at` | TEXT | N |  |  |
| `session_ref` | TEXT | Y |  | 0008 추가. 7.8절 |

### 가져오기(import) 확장(마이그레이션 0005)

#### `import_segment`

원본 파일을 나눈 구간과 처리 상태. 7.4절

| 컬럼 | 타입 | NULL | 키·제약 | 설명 |
|---|---|---|---|---|
| `seg_id` | INTEGER | N | PK |  |
| `artifact_id` | TEXT | N | → artifact.artifact_id |  |
| `seq` | INTEGER | N |  |  |
| `heading` | TEXT | Y |  |  |
| `start_off` | INTEGER | N |  |  |
| `end_off` | INTEGER | N |  |  |
| `text_digest` | TEXT | N |  |  |
| `status` | TEXT | N | CHECK (status IN ('pending','ruled','labeled','skipped','applied','stale')) |  |
| `kind_id` | TEXT | Y | → cat_kind_field.kind_id |  |
| `field_key` | TEXT | Y | → cat_kind_field.field_key |  |
| `doc_id` | TEXT | Y | → document.doc_id |  |
| `rule` | TEXT | Y |  |  |
| `why` | TEXT | Y |  |  |

테이블 제약:

- `UNIQUE (artifact_id, seq)`
- `FOREIGN KEY (kind_id, field_key) REFERENCES cat_kind_field(kind_id, field_key)`

## 4. 뷰

| 뷰 | 설명 | 읽는 테이블·뷰 | 출처 |
|---|---|---|---|
| `v_document_register` | A01 문서 마스터 목록 | `document`, `document_kind` | schema |
| `v_missing_required_fields` | 필수 필드 누락 (FIELD_MISSING) | `cat_kind`, `cat_kind_field`, `document_kind`, `field_value` | schema |
| `v_rtm` | D08 요구사항 추적 (REQ → 구현·검증). 0003에서 `status = 'confirmed'`인 관계만 보도록 다시 정의 | `item`, `relation` | schema, 0003 |
| `v_doc_head` | 최신 문서 리비전 (head) | `doc_revision` | schema |
| `v_recent_changes` | 최근 변경 (누가·무엇을·어느 작업에서) — 사람/AI 구분 | `actor`, `change_log` | schema |
| `v_entity_head` | 엔터티별 최신 변경 (링크드 리스트의 head) | `change_log` | schema |
| `v_context_stale` | AI가 읽은 뒤 바뀐 근거 (낡은 컨텍스트) — 재개·인계 전에 다시 읽어야 할 목록 | `change_log`, `context_read` | schema |
| `v_work_resume` | 작업 재개 카드: 상태·마지막 인계·미해결 지적·미실행/실패 실행·낡은 근거 수 | `change_log`, `finding`, `handoff`, `run`, `v_context_stale`, `work` | schema |
| `v_summary_stale` | 다시 요약해야 할 목록 (배치로 처리) | `entity_summary`, `field_value` | token_ext |
| `v_text_unit` | 검색 색인의 본문 원천(FTS5 external content가 읽는 뷰) | `field_value`, `item`, `text_unit` | token_ext |
| `v_l0_document` | LLM용 한 줄 표현(L0) — 문서 | `document`, `document_kind` | token_ext |
| `v_missing_required_links` | 필수 연결 누락(7.2절) | `cat_kind_link`, `document`, `document_kind`, `item`, `link_exemption`, `relation` | 0003 |

## 5. 트리거

| 트리거 | 대상 | 시점 | 규칙 | 오류 메시지 | 마이그레이션 |
|---|---|---|---|---|---|
| `item_id_prefix` | `item` | BEFORE INSERT | 항목 ID는 종류 접두어로 시작 | item_id must start with the item_type prefix | 0001 |
| `context_read_no_update` | `context_read` | BEFORE UPDATE | AI 읽기 기록 수정 금지(append-only) | context_read is append-only | 0001 |
| `change_log_no_update` | `change_log` | BEFORE UPDATE | append-only 강제 | change_log is append-only; add a correct record | 0001 |
| `change_log_no_delete` | `change_log` | BEFORE DELETE | append-only 강제 | change_log is append-only | 0001 |
| `doc_revision_no_update` | `doc_revision` | BEFORE UPDATE | 기준선 불변 | doc_revision is immutable; create a new revision | 0001 |
| `doc_revision_no_delete` | `doc_revision` | BEFORE DELETE | 기준선 불변 | doc_revision is immutable | 0001 |
| `run_no_update` | `run` | BEFORE UPDATE | 실행 증거 불변(정정은 새 실행으로) | run evidence is immutable; record a new run | 0001 |
| `change_log_chain_check` | `change_log` | BEFORE INSERT | 링크는 같은 엔터티/문서 안에서만, 순번은 직전+1 | broken chain: prev must be same entity, seq-1, matching digest | 0001 |
| `doc_revision_chain_check` | `doc_revision` | BEFORE INSERT | 기준선 체인: 같은 문서, 직전 번호, digest 일치 | broken chain: prev must be same doc, revision_no-1, matching digest | 0001 |
| `document_kind_lifecycle` | `document_kind` | BEFORE INSERT | 한 문서에 living/work 종류 혼합 금지 (AT-29) | kind lifecycle must match document lifecycle | 0001 |
| `approval_human_only` | `approval` | BEFORE INSERT | 승인자는 사람만 | AI actor cannot approve | 0001 |
| `document_legal_hold` | `document` | BEFORE DELETE | legal_hold 문서 삭제 금지 | legal_hold document cannot be deleted | 0001 |
| `fv_fts_ai` | `field_value` | AFTER INSERT | 검색 색인(fts_text) 동기화 | — | 0002 |
| `fv_fts_au` | `field_value` | AFTER UPDATE | 검색 색인(fts_text) 동기화 | — | 0002 |
| `fv_fts_ad` | `field_value` | AFTER DELETE | 검색 색인(fts_text) 동기화 | — | 0002 |
| `item_fts_ai` | `item` | AFTER INSERT | 검색 색인(fts_text) 동기화 | — | 0002 |
| `item_fts_au` | `item` | AFTER UPDATE | 검색 색인(fts_text) 동기화 | — | 0002 |
| `relation_source_status` | `relation` | BEFORE INSERT | 출처·상태·제안자 조합 검사(7.1절) | relation source/status/proposer mismatch | 0003 |
| `relation_decide_human_only` | `relation` | BEFORE UPDATE OF status | `proposed`에서만 전이, 결정자는 사람만(7.1절) | only a human can decide a proposed relation | 0003 |
| `item_fts_ad` | `item` | AFTER DELETE | 검색 색인(fts_text) 동기화(7.3절) | — | 0004 |
| `document_id_format` | `document` | BEFORE INSERT | `doc_id` 형식(7.6절) | doc_id format invalid | 0007 |
| `item_id_format` | `item` | BEFORE INSERT | `item_id` 형식(7.6절) | item_id format invalid | 0007 |
| `run_id_format` | `run` | BEFORE INSERT | `run_id` 형식(7.6절) | run_id format invalid | 0007 |
| `handoff_id_format` | `handoff` | BEFORE INSERT | `handoff_id` 형식(7.6절) | handoff_id format invalid | 0007 |
| `approval_id_format` | `approval` | BEFORE INSERT | `approval_id` 형식(7.6절) | approval_id format invalid | 0007 |
| `artifact_id_format` | `artifact` | BEFORE INSERT | `artifact_id` 형식(7.6절) | artifact_id format invalid | 0007 |
| `work_id_format` | `work` | BEFORE INSERT | `work_id` 형식(7.6절) | work_id format invalid | 0007 |

## 6. 시드 데이터

시드 파일 `docdb/catalogue/item_types.json`: 항목 종류(`cat_item_type`) 39행(`known_error`를 뺀 38행에 `defined_by_field`가 있다). 시작 카탈로그에 없는 종류를 참조하는 행은 적재하지 않는다([07](07-catalogue.md) 4절).

| item_type | 접두어 | 정의 문서 종류 | 설명 |
|---|---|---|---|
| `term` | `TERM-` | A04 | 용어·약어 (A04.term_id) |
| `work_package` | `WBS-` | B02 | 작업 분해 항목 (B02.items) |
| `role` | `ROLE-` | B03 | 역할 (B03.roles, Z01.teams) |
| `raid` | `RAID-` | B04 | 리스크·이슈·가정·의존성 (B04.items) |
| `feature` | `FEAT-` | C04 | 제품 기능 (C04.features) |
| `metric` | `MET-` | J08 | 업무 지표 (J08.metric_id, C06.metric_ids) |
| `requirement` | `REQ-` | D01 | 기능 요구사항 (D01.requirement_ids) |
| `acceptance_criterion` | `AC-` | D01 | 완료 조건 (D01.acceptance) |
| `business_rule` | `RULE-` | D03 | 업무 규칙·계산식 (D03.rule_ids) |
| `state` | `ST-` | D04 | 상태 (D04.states) |
| `error_code` | `ERR-` | D05 | 오류 코드 (D05.error_id, H02.errors) |
| `nfr` | `NFR-` | D06 | 비기능 요구 (D06.nfr_ids) |
| `screen` | `SCR-` | E01 | 화면 (E01.screen_ids, E02.screen_id, E03.nodes) |
| `ui_message` | `MSG-` | E04 | 화면 메시지 (E04.messages) |
| `design_token` | `TOK-` | E05 | 디자인 토큰 (E05.tokens) |
| `a11y_check` | `A11Y-` | E06 | 접근성 검사 항목 (E06.criteria) |
| `analytics_event` | `AEV-` | E08 | 분석 이벤트 (E08.event_id) |
| `module` | `MOD-` | F02 | 모듈 (F02.modules) |
| `scenario` | `SCN-` | F03 | 실행 시나리오 (F03.scenarios) |
| `tech_debt` | `DEBT-` | F07 | 기술부채 (F07.debt_id) |
| `api_operation` | `API-` | H01 | API operation (H01.operations) |
| `message_contract` | `MEV-` | H05 | 이벤트·메시지 계약 (H05.events) |
| `dataset` | `DS-` | J01 | 데이터셋 (J01.datasets, J02.dataset_id) |
| `environment` | `ENV-` | K01 | 환경 (K01.environment_ids, L04.environments) |
| `infra_resource` | `RES-` | K02 | 서버·클러스터·하드웨어 자원 (K02.resources) |
| `asset` | `AST-` | S03 | 형상·자산 (S03.assets) |
| `test_case` | `TC-` | L02 | 테스트 케이스 (L02.test_ids) |
| `defect` | `DEF-` | L07 | 결함 (L07.defects) |
| `vulnerability` | `VULN-` | M09 | 취약점 (M09.findings) |
| `feature_flag` | `FLAG-` | O06 | 기능 플래그 (O06.flags) |
| `service` | `SVC-` | P01 | 서비스 (P01.services) |
| `dashboard` | `DASH-` | P03 | 대시보드 (P03.dashboards) |
| `log_event` | `LOG-` | P04 | 로그 이벤트 코드 (P04.events) |
| `job` | `JOB-` | P07 | 배치·스케줄 작업 (P07.jobs) |
| `incident` | `INC-` | Q03 | 장애 (Q03.incidents) |
| `known_error` | `KE-` | Q03 | Known Error (Q03) |
| `support_case` | `CASE-` | T07 | 지원 요청·공지 (T07.case) |
| `contract` | `CTR-` | U04 | 계약 (U04.contracts) |
| `ai_model` | `AIM-` | V01 | AI 모델·에이전트 (V01.identity) |

## 7. 적용된 변경 (2026-10-13 결정 반영, 마이그레이션 0003~0008)

아래 변경은 모두 마이그레이션 파일로 적용되어 있다(구현 계획 [plan/](plan/README.md)). 이 문서의 1~6절은 적용 결과를 실제 DB에서 읽어 생성했다. 모두 확정된 결정에서 나온 변경이다.

### 7.0 마이그레이션 번호

`PRAGMA user_version`이 마지막으로 적용한 번호다(D21). 현재 값은 8이다. 적용한 파일은 고치지 않고 새 번호로만 추가한다.

| 번호 | 파일 | 내용 | 결정 |
|---|---|---|---|
| 1 | `0001_base.sql` | 원본 `docdb_schema.sql` 그대로 | 원 설계 |
| 2 | `0002_token_ext.sql` | 원본 `docdb_schema_token_ext.sql` 그대로 | 원 설계 |
| 3 | `0003_relation_status.sql` | 7.1 컬럼·트리거, 7.2 뷰, `v_rtm`을 확정 관계만 보도록 재정의 | D10 |
| 4 | `0004_item_fts_ad.sql` | 7.3 | D7 |
| 5 | `0005_import_segment.sql` | 7.4 | D6 |
| 6 | `0006_catalogue_ext.sql` | 7.5 `extract_rel`, 7.9 `defined_by_field` | D10 |
| 7 | `0007_id_formats.sql` | 7.6 | D17 |
| 8 | `0008_call_log_session.sql` | 7.8 | D18 |

표의 번호와 파일은 `docdb/src/docdb/schema/`의 실제 파일과 같다. 7.1~7.3은 0003·0004, 7.4는 0005, 7.5·7.9는 0006, 7.6은 0007, 7.8은 0008에 들어갔다. 7.10은 마이그레이션이 없다.

### 7.1 `relation` 상태 컬럼과 트리거 — D10 확정, 적용됨(0003)

| 추가 컬럼 | 타입 | 제약 | 설명 |
|---|---|---|---|
| `status` | TEXT | NOT NULL DEFAULT `'confirmed'` CHECK IN (`proposed`,`confirmed`,`rejected`) | 확정 여부 |
| `source` | TEXT | NOT NULL | `rule:<규칙 ID>`(스크립트 추출) / `agent` / `human` |
| `proposed_by` | TEXT | → `actor.actor_id` | 제안자 |
| `decided_by` | TEXT | → `actor.actor_id` | 확정·반려한 사람 |
| `decided_at` | TEXT | | 확정·반려 시각 |

| 추가 트리거 | 시점 | 규칙 |
|---|---|---|
| `relation_source_status` | BEFORE INSERT ON `relation` | `source='agent'` → `status='proposed'`이고 `proposed_by`는 `actor_type='ai'`. `source='human'` → `proposed_by`는 `actor_type='human'`. `source LIKE 'rule:%'` → `proposed_by IS NULL`, `status='confirmed'`. 그 밖의 조합은 거부 |
| `relation_decide_human_only` | BEFORE UPDATE OF `status` ON `relation` | `proposed`에서만 전이 가능, `decided_by`가 `actor_type='human'`이 아니면 거부 |

규칙 추출(R1~R5)은 에이전트의 `put`이 계기가 되어도 행위자를 규칙(`source='rule:<ID>'`)으로 기록한다. 규칙은 같은 입력이면 같은 결과를 내므로 AI 판단이 아니다. `source` 값은 docctl이 정하며 에이전트 입력으로 받지 않는다. SQL을 직접 실행하면 `source`를 위장할 수 있으므로, 이 트리거는 스크립트 버그와 실수를 막는 장치이고 보안 경계는 아니다.

삭제 주체는 DB가 알 수 없다(DELETE 문에 행위자 정보가 없음). 그래서 "확정된 관계 삭제는 사람만" 규칙은 docctl이 강제한다(AI 경로는 자기 `proposed`만 철회). 같은 (출발, 종류, 도착)을 다시 제안하면 기존 UNIQUE 제약에 걸리고, docctl이 `REJECTED_BEFORE`·중복으로 바꿔 응답한다.

조회 변경: `v_rtm`과 `trace`·`ctx`의 관계 조회에 `status='confirmed'` 조건을 추가한다.

### 7.2 필수 연결 검사 뷰 `v_missing_required_links` — D10 확정(B2), 적용됨(0003)

적용 대상 문서마다 `cat_kind_link(is_required=1)`의 대상 종류와 확정된 연결이 없고 `link_exemption`도 없는 행을 낸다. "종류 Y와 연결됨"은 확정된 `relation`의 상대가 (a) 종류 Y 문서이거나 (b) 종류 Y 문서에서 정의된 항목(`item.defined_in`)인 경우다.

| 출력 컬럼 | 설명 |
|---|---|
| `doc_id`, `kind_id` | 검사 대상 문서와 종류 |
| `target_kind_id` | 연결이 빠진 대상 종류 |

### 7.3 `item` 검색 색인 삭제 트리거 — D7 확정(P4), 적용됨(0004)

| 추가 트리거 | 시점 | 동작 |
|---|---|---|
| `item_fts_ad` | AFTER DELETE ON `item` | `fts_text` 색인 항목 삭제(`fv_fts_ad`와 같은 방식). `text_unit` 행은 남겨 같은 ID를 다시 만들 때 재사용 |

### 7.4 `import_segment` 테이블 — D6 확정, 적용됨(0005)

| 컬럼 | 타입 | 제약 | 설명 |
|---|---|---|---|
| `seg_id` | INTEGER | PK | 구간 ID(`s:<id>` 핸들) |
| `artifact_id` | TEXT | NOT NULL → `artifact` | 원본 파일 |
| `seq` | INTEGER | NOT NULL, UNIQUE(`artifact_id`,`seq`) | 파일 안 순서 |
| `heading` | TEXT | | 구간 제목(0번 구간은 NULL) |
| `start_off`, `end_off` | INTEGER | NOT NULL | 원본 안 바이트 위치 |
| `text_digest` | TEXT | NOT NULL | 구간 원문 digest(적용 시 다시 계산해 비교) |
| `status` | TEXT | NOT NULL CHECK IN (`pending`,`ruled`,`labeled`,`skipped`,`applied`,`stale`) | 처리 상태 |
| `kind_id`, `field_key` | TEXT | → `cat_kind_field` | 매핑 결과 |
| `doc_id` | TEXT | → `document` | 적용된 문서 |
| `rule` | TEXT | | 규칙 매핑이면 규칙 ID, 라벨이면 `agent` |
| `why` | TEXT | | 건너뜀 사유 등 |

scan 1회는 기존 `run`에 `run_type='import'`로 남긴다.

### 7.5 `cat_kind_field.extract_rel` — D10 확정(R3), 원 카탈로그에 없는 확장, 적용됨(0006)

| 추가 컬럼 | 타입 | 설명 |
|---|---|---|
| `extract_rel` | TEXT CHECK(`extract_rel IS NULL OR json_valid(extract_rel)`) | 이 필드 본문의 **같은 줄**에 함께 나온 두 종류 ID 사이에 만들 관계. 형식: `{"rel":"verified_by","from_prefix":"REQ-","to_prefix":"TC-"}` — 한 줄에 `REQ-…`와 `TC-…`가 함께 있으면 `REQ verified_by TC`(규칙 R3) |

### 7.6 ID 형식 트리거 — D17, 적용됨(0007)

| 대상 | 형식(Python 정규식, 트리거는 같은 조건을 GLOB으로 구현) | 발급 |
|---|---|---|
| `document.doc_id` | `^DOC-[A-Z0-9]+(-[A-Z0-9]+)*$`, 64자 이하 | 사람·에이전트가 의미 있는 이름 지정. import는 자동 규칙 |
| `run.run_id` | `^RUN-\d{4}-\d{4,}$` | 연도별 순번 자동 |
| `handoff.handoff_id` | `^HO-\d{4}-\d{4,}$` | 연도별 순번 자동 |
| `approval.approval_id` | `^APR-\d{4}-\d{4,}$` | 연도별 순번 자동 |
| `artifact.artifact_id` | `^ART-\d{4}-\d{4,}$` | 연도별 순번 자동 |
| `work.work_id` | 기존 CHECK `WORK-[0-9][0-9][0-9][0-9]-[0-9]*`를 유지하고 `work_id_format` 트리거가 `^WORK-\d{4}-\d{4,}$`를 추가로 검사 | 연도별 순번 자동 |
| `item.item_id` | 기존 `item_id_prefix` 트리거 + `^[A-Z0-9]+-[A-Z0-9]+(-[A-Z0-9]+)*$` | 의미 있는 이름 |

트리거 이름: `<table>_id_format` (BEFORE INSERT, 7개: `document` `item` `run` `handoff` `approval` `artifact` `work`). 오류 메시지: `<column> format invalid`. `item`은 기존 `item_id_prefix` 트리거도 함께 유지한다.

### 7.7 시제품 SQL과 검증 결과 (0003·0004의 출발점)

7.1~7.3의 변경을 원본 스키마(+토큰 확장, 미니 카탈로그)에 적용해 SQLite 3.53.1에서 확인했다(2026-10-13, 10/10 통과). 이 SQL은 마이그레이션 0003·0004의 출발점으로 쓰였다. 아래 SQL은 시제품 그대로이며 `relation`의 NOT NULL 컬럼 추가는 기본값이 필요해 `source`에 `'human'` 기본값을 두었다. 적용된 0003도 `source`의 기본값이 `'human'`이다(`rule:legacy`로 채우는 재작성은 하지 않았다). 0003은 이 SQL에 `v_rtm`을 확정 관계만 보도록 다시 정의하는 `DROP VIEW`·`CREATE VIEW`를 더했다.

| 검사 | 결과 |
|---|---|
| AI가 `confirmed`로 삽입 | 거부 |
| AI가 규칙 출처로 위장(`proposed_by` 있음) | 거부 |
| AI 제안(`proposed`) | 허용 |
| AI가 확정 | 거부 |
| 사람이 확정 | 허용 |
| 확정 후 반려로 전이 | 거부 |
| 규칙 추출(`confirmed`, `proposed_by` NULL) | 허용 |
| 같은 조합 재제안 | UNIQUE 거부 |
| `v_missing_required_links` | 연결 없음 1 → 제안만 있음 1 → 확정 후 0 |
| `item_fts_ad` | 삭제 후 검색 1 → 0, FTS 무결성 검사 통과 |

```sql
ALTER TABLE relation ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'
  CHECK (status IN ('proposed','confirmed','rejected'));
ALTER TABLE relation ADD COLUMN source TEXT NOT NULL DEFAULT 'human';
ALTER TABLE relation ADD COLUMN proposed_by TEXT REFERENCES actor(actor_id);
ALTER TABLE relation ADD COLUMN decided_by TEXT REFERENCES actor(actor_id);
ALTER TABLE relation ADD COLUMN decided_at TEXT;

CREATE TRIGGER relation_source_status BEFORE INSERT ON relation
WHEN NOT (
     (NEW.source = 'agent' AND NEW.status = 'proposed'
      AND (SELECT actor_type FROM actor WHERE actor_id = NEW.proposed_by) = 'ai')
  OR (NEW.source = 'human'
      AND (SELECT actor_type FROM actor WHERE actor_id = NEW.proposed_by) = 'human')
  OR (NEW.source LIKE 'rule:%' AND NEW.proposed_by IS NULL AND NEW.status = 'confirmed'))
BEGIN SELECT RAISE(ABORT, 'relation source/status/proposer mismatch'); END;

CREATE TRIGGER relation_decide_human_only BEFORE UPDATE OF status ON relation
WHEN NEW.status <> OLD.status
 AND (OLD.status <> 'proposed'
      OR coalesce((SELECT actor_type FROM actor WHERE actor_id = NEW.decided_by), '') <> 'human')
BEGIN SELECT RAISE(ABORT, 'only a human can decide a proposed relation'); END;

CREATE TRIGGER item_fts_ad AFTER DELETE ON item BEGIN
  INSERT INTO fts_text (fts_text, rowid, body)
  SELECT 'delete', unit_id, OLD.title || ' ' || OLD.attrs FROM text_unit WHERE item_id = OLD.item_id;
END;

CREATE VIEW v_missing_required_links AS
WITH ends AS (
  SELECT from_type AS a_type, from_id AS a_id, to_type AS b_type, to_id AS b_id FROM relation WHERE status = 'confirmed'
  UNION ALL
  SELECT to_type, to_id, from_type, from_id FROM relation WHERE status = 'confirmed'),
owner AS (
  SELECT 'document' AS t, doc_id AS id, doc_id FROM document
  UNION ALL
  SELECT 'item', item_id, defined_in FROM item WHERE defined_in IS NOT NULL),
linked AS (
  SELECT oa.doc_id AS doc_id, ob.doc_id AS other_doc
  FROM ends e
  JOIN owner oa ON oa.t = e.a_type AND oa.id = e.a_id
  JOIN owner ob ON ob.t = e.b_type AND ob.id = e.b_id)
SELECT dk.doc_id, dk.kind_id, l.target_kind_id
FROM document_kind dk
JOIN document d ON d.doc_id = dk.doc_id AND d.applicability_status <> 'not_applicable'
JOIN cat_kind_link l ON l.kind_id = dk.kind_id AND l.is_required = 1
WHERE NOT EXISTS (SELECT 1 FROM link_exemption e WHERE e.doc_id = dk.doc_id AND e.target_kind_id = l.target_kind_id)
  AND NOT EXISTS (SELECT 1 FROM linked k JOIN document_kind dk2 ON dk2.doc_id = k.other_doc
                  WHERE k.doc_id = dk.doc_id AND dk2.kind_id = l.target_kind_id);
```

### 7.8 `call_log.session_ref` — D18, 적용됨(0008)

| 추가 컬럼 | 타입 | 설명 |
|---|---|---|
| `session_ref` | TEXT | 에이전트 세션·모델 표시. `DOCDB_SESSION`과 `DOCDB_MODEL` 환경 변수를 `<session>/<model>` 형식으로 기록 |

### 7.9 `cat_item_type.defined_by_field` — D10(R1), 적용됨(0006)

| 추가 컬럼 | 타입 | 설명 |
|---|---|---|
| `defined_by_field` | TEXT | 이 항목 종류를 정의하는 필드 키(예: `requirement`는 D01의 `requirement_ids`). 규칙 R1은 이 필드에 쓴 ID만 항목으로 만든다 |

### 7.10 변경 없음으로 처리하는 결정(마이그레이션 없음)

| 결정 | 이유 |
|---|---|
| D8 외부 전송 정책 필터 | 기존 `document.export_policy` 사용. 읽기 경로(docctl)에서 처리 |
| D15 작업·인계 쓰기 | 기존 `work`·`handoff` 테이블 사용. 쓰기 경로만 추가 |
| D16 요약 동봉 | 기존 `entity_summary`(`author_type='llm'`) 사용 |

## 8. 이 문서의 재생성

마이그레이션 파일이 바뀌면 다시 생성한다. 생성 절차: 빈 DB에 `docdb.migrate.apply_pending`을 적용(SQLite 3.34 이상, trigram 지원) → `PRAGMA user_version`·`table_xinfo`·`foreign_key_list`·`sqlite_master` 조회 → SQL 주석을 설명 열로 사용. 2026-10-14 T30은 PRAGMA 조회 결과와 이 문서를 대조하는 임시 스크립트로 갱신했으며, 상시 생성 스크립트는 저장소에 없다.

