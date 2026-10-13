-- =====================================================================
-- docdb_schema_token_ext.sql — 토큰 절감용 확장 (docdb_schema.sql 적용 후 실행)
-- 목적: 저장은 세밀하게, LLM이 읽는 양은 최소로.
--   1) 변경 감지용 digest·토큰 추정치(tok_est)를 값 옆에 둔다 → 내용을 읽지 않고 예산 계산·"변경 없음" 응답
--   2) L1 요약을 파생물로 둔다 (원본 digest로 낡음 판정)
--   3) 전문 검색 색인 (FTS5 external content: 본문을 복사하지 않음)
--   4) 호출 로그 → 어떤 호출이 토큰을 많이 쓰는지 측정
-- 기존 테이블은 컬럼 추가만 한다(ALTER ADD COLUMN). 기존 테스트에 영향 없음.
-- =====================================================================

-- 1) 값 옆의 digest·토큰 추정치 (스크립트가 put 시 계산. SQLite에는 sha256 함수가 없음)
ALTER TABLE field_value ADD COLUMN digest  TEXT;     -- sha256 앞 16자리면 충분 (프로젝트 내 비교용)
ALTER TABLE field_value ADD COLUMN tok_est INTEGER;  -- 본문 토큰 추정치 (보정 전 휴리스틱)
ALTER TABLE item        ADD COLUMN digest  TEXT;
ALTER TABLE item        ADD COLUMN tok_est INTEGER;

-- 2) L1 요약 — 원본이 아닌 파생물. 원본 digest가 바뀌면 낡음
CREATE TABLE entity_summary (
  handle        TEXT PRIMARY KEY,                -- 'f:DOC-X/D01/behavior', 'i:REQ-1', 'd:DOC-X'
  summary       TEXT NOT NULL,                   -- 1~3문장. 사실·숫자·ID 보존
  source_digest TEXT NOT NULL,                   -- 요약 당시 원본 digest
  tok_est       INTEGER NOT NULL,
  author_type   TEXT NOT NULL CHECK (author_type IN ('script','llm')),
  author_id     TEXT NOT NULL REFERENCES actor(actor_id),
  created_at    TEXT NOT NULL
);

CREATE VIEW v_summary_stale AS                   -- 다시 요약해야 할 목록 (배치로 처리)
SELECT s.handle, s.source_digest, f.digest AS current_digest
FROM entity_summary s
JOIN field_value f ON s.handle = 'f:' || f.doc_id || '/' || f.kind_id || '/' || f.field_key
WHERE f.digest IS NOT s.source_digest;

-- 3) 전문 검색 — 검색 단위 표 + external content FTS5
--    필드값의 암묵 rowid는 VACUUM으로 바뀔 수 있으므로(sqlite lang_vacuum §3)
--    명시적 INTEGER PRIMARY KEY를 가진 text_unit을 rowid로 쓴다.
CREATE TABLE text_unit (
  unit_id   INTEGER PRIMARY KEY,
  src       TEXT NOT NULL CHECK (src IN ('f','i')),
  doc_id    TEXT, kind_id TEXT, field_key TEXT,  -- src='f'
  item_id   TEXT,                                -- src='i'
  UNIQUE (doc_id, kind_id, field_key),
  UNIQUE (item_id)
);

CREATE VIEW v_text_unit AS
SELECT u.unit_id, coalesce(f.value_text, f.value_json, '') AS body
FROM text_unit u JOIN field_value f
  ON f.doc_id = u.doc_id AND f.kind_id = u.kind_id AND f.field_key = u.field_key
WHERE u.src = 'f'
UNION ALL
SELECT u.unit_id, i.title || ' ' || i.attrs AS body
FROM text_unit u JOIN item i ON i.item_id = u.item_id
WHERE u.src = 'i';

-- trigram: 부분 문자열 검색(한국어 조사 붙은 단어 대응). 3글자 미만 검색어는 MATCH 불가 → 스크립트가 LIKE로 대체
CREATE VIRTUAL TABLE fts_text USING fts5(body, content='v_text_unit', content_rowid='unit_id', tokenize='trigram');

CREATE TRIGGER fv_fts_ai AFTER INSERT ON field_value BEGIN
  INSERT OR IGNORE INTO text_unit (src, doc_id, kind_id, field_key) VALUES ('f', NEW.doc_id, NEW.kind_id, NEW.field_key);
  INSERT INTO fts_text (rowid, body)
  SELECT unit_id, coalesce(NEW.value_text, NEW.value_json, '') FROM text_unit
  WHERE doc_id = NEW.doc_id AND kind_id = NEW.kind_id AND field_key = NEW.field_key;
END;
CREATE TRIGGER fv_fts_au AFTER UPDATE OF value_text, value_json ON field_value BEGIN
  INSERT INTO fts_text (fts_text, rowid, body)
  SELECT 'delete', unit_id, coalesce(OLD.value_text, OLD.value_json, '') FROM text_unit
  WHERE doc_id = OLD.doc_id AND kind_id = OLD.kind_id AND field_key = OLD.field_key;
  INSERT INTO fts_text (rowid, body)
  SELECT unit_id, coalesce(NEW.value_text, NEW.value_json, '') FROM text_unit
  WHERE doc_id = NEW.doc_id AND kind_id = NEW.kind_id AND field_key = NEW.field_key;
END;
CREATE TRIGGER fv_fts_ad AFTER DELETE ON field_value BEGIN
  INSERT INTO fts_text (fts_text, rowid, body)
  SELECT 'delete', unit_id, coalesce(OLD.value_text, OLD.value_json, '') FROM text_unit
  WHERE doc_id = OLD.doc_id AND kind_id = OLD.kind_id AND field_key = OLD.field_key;
END;
CREATE TRIGGER item_fts_ai AFTER INSERT ON item BEGIN
  INSERT OR IGNORE INTO text_unit (src, item_id) VALUES ('i', NEW.item_id);
  INSERT INTO fts_text (rowid, body)
  SELECT unit_id, NEW.title || ' ' || NEW.attrs FROM text_unit WHERE item_id = NEW.item_id;
END;
CREATE TRIGGER item_fts_au AFTER UPDATE OF title, attrs ON item BEGIN
  INSERT INTO fts_text (fts_text, rowid, body)
  SELECT 'delete', unit_id, OLD.title || ' ' || OLD.attrs FROM text_unit WHERE item_id = OLD.item_id;
  INSERT INTO fts_text (rowid, body)
  SELECT unit_id, NEW.title || ' ' || NEW.attrs FROM text_unit WHERE item_id = NEW.item_id;
END;

-- 4) 호출 로그 — 토큰을 어디에 썼는지 측정 (context_read는 '무엇을 읽었나', call_log는 '얼마나 실었나')
CREATE TABLE call_log (
  call_id    INTEGER PRIMARY KEY,
  tool       TEXT NOT NULL,                      -- ctx/get/find/trace/since/put
  args_json  TEXT NOT NULL CHECK (json_valid(args_json)),
  actor_id   TEXT REFERENCES actor(actor_id),
  work_id    TEXT REFERENCES work(work_id),
  out_chars  INTEGER NOT NULL,
  out_tok_est INTEGER NOT NULL,
  budget     INTEGER,
  omitted    INTEGER NOT NULL DEFAULT 0,          -- 예산 때문에 생략한 항목 수
  called_at  TEXT NOT NULL
);

-- LLM용 한 줄 표현(L0) — 문서
CREATE VIEW v_l0_document AS
SELECT d.doc_id,
       'D ' || d.doc_id || ' ' || coalesce(group_concat(dk.kind_id, ','), '-') || ' ' || d.status
       || ' "' || d.title || '"' AS line
FROM document d LEFT JOIN document_kind dk ON dk.doc_id = d.doc_id
GROUP BY d.doc_id;

CREATE INDEX ix_change_work ON change_log(work_id, change_id);
CREATE INDEX ix_doc_work    ON document(work_id);
CREATE INDEX ix_doc_origin  ON document(origin_work_id);
CREATE INDEX ix_item_defined ON item(defined_in);
