"""스키마 동작 검증: 표준의 인수 테스트(AT) 중 DB 제약으로 강제 가능한 항목 + 이력 링크드 리스트"""
import sys, json, hashlib, sqlite3
from load_catalogue import load
con, d = load(sys.argv[1], sys.argv[2], sys.argv[3])
con.execute('PRAGMA foreign_keys=ON')
NOW = '2026-10-08T18:00:00+09:00'
R = []
def expect_fail(name, sql, args=()):
    try:
        con.execute(sql, args); R.append((name, 'FAIL(허용됨)')); con.rollback()
    except sqlite3.DatabaseError as e:
        R.append((name, f'OK 차단: {str(e)[:70]}'))
def ok(name, cond, detail=''):
    R.append((name, ('OK ' if cond else 'FAIL ') + detail))

x = con.execute
x("INSERT INTO actor VALUES ('u-jjm','human','주정민',1),('team-ops','team','운영팀',1),('ai-codex','ai','Codex',1)")
x("INSERT INTO project (project_id,name,catalogue_version) VALUES ('PRJ-ITEASY','프로덕션 SW 관리',?)", (d['standard_version'],))
# ---- 프로필: 세 값 논리 (AT-01~03)
x("INSERT INTO fact_value (project_id,fact_key,value,reason,confirmed_by,confirmed_at) VALUES "
  "('PRJ-ITEASY','cap.database',1,'운영 DB 사용','u-jjm',?),('PRJ-ITEASY','cap.ui',0,'화면 없음','u-jjm',?),"
  "('PRJ-ITEASY','cap.production',NULL,NULL,NULL,NULL)", (NOW, NOW))
facts = {k: v for k, v in x("SELECT fact_key, value FROM fact_value")}
def ev(rule):   # 제한 연산만 지원 (eval 금지, 10.6)
    if 'always' in rule: return True
    if 'fact' in rule:
        v = facts.get(rule['fact']); return None if v is None else bool(v)
    if 'selected_kind' in rule: return False
    if 'all' in rule:
        vs = [ev(r) for r in rule['all']]
        return False if False in vs else (True if all(v is True for v in vs) else None)
    if 'any' in rule:
        vs = [ev(r) for r in rule['any']]
        return True if True in vs else (False if all(v is False for v in vs) else None)
    raise ValueError(rule)
t = lambda v: {True: 'true', False: 'false', None: 'unknown'}[v]
for k in d['document_types']:
    a, r = ev(k['applies_when']), ev(k['required_when'])
    x("INSERT INTO kind_applicability VALUES ('PRJ-ITEASY',?,?,?,?,1,?)", (k['kind_id'], t(a), t(r), json.dumps(k['required_when'], ensure_ascii=False), NOW))
g = lambda kid: x("SELECT required FROM kind_applicability WHERE kind_id=?", (kid,)).fetchone()[0]
ok('AT-01 DB=true → I01 필수', g('I01') == 'true', f"I01.required={g('I01')} (문서 없으면 DOC_MISSING)")
ok('AT-02 UI=false → E01 비필수', g('E01') == 'false', f"E01.required={g('E01')}")
ok('AT-03 production=NULL → F04 미확인', g('F04') == 'unknown', f"F04.required={g('F04')} (FACT_UNKNOWN)")
cnt = dict(x("SELECT required, count(*) FROM kind_applicability GROUP BY 1").fetchall())
ok('적용 판정 194종 집계', sum(cnt.values()) == 194, str(cnt))

# ---- 문서·항목·관계
x("INSERT INTO work VALUES ('WORK-2026-0001','PRJ-ITEASY','feature','서버 점검 자동화','active','u-jjm','[]','[]',?,NULL)", (NOW,))
x("""INSERT INTO document (doc_id,project_id,title,lifecycle,category,authority,baseline_mode,owner_id,created_at,updated_at)
     VALUES ('DOC-REQ-HWCHECK','PRJ-ITEASY','서버 점검 요구사항','living','D','normative','desired','u-jjm',?,?)""", (NOW, NOW))
x("INSERT INTO document_kind VALUES ('DOC-REQ-HWCHECK','D01')")
expect_fail('AT-04 doc_id 중복', "INSERT INTO document (doc_id,project_id,title,lifecycle,category,authority,baseline_mode,owner_id,created_at,updated_at) VALUES ('DOC-REQ-HWCHECK','PRJ-ITEASY','x','living','D','normative','desired','u-jjm','t','t')")
expect_fail('AT-29 living 문서에 work 종류(F05) 혼합', "INSERT INTO document_kind VALUES ('DOC-REQ-HWCHECK','F05')")
x("INSERT INTO field_value (doc_id,kind_id,field_key,fill_state,value_text,author_id,updated_at) VALUES ('DOC-REQ-HWCHECK','D01','requirement_ids','filled','REQ-HW-001','ai-codex',?)", (NOW,))
miss = [r[2] for r in x("SELECT * FROM v_missing_required_fields WHERE doc_id='DOC-REQ-HWCHECK'")]
ok('AT-05 필수 필드 누락 탐지', set(miss) == {'behavior', 'acceptance', 'scope', 'owners'}, f'누락={miss}')
expect_fail('필드 tbd인데 사유 없음', "INSERT INTO field_value (doc_id,kind_id,field_key,fill_state,author_id,updated_at) VALUES ('DOC-REQ-HWCHECK','D01','scope','tbd','u-jjm','t')")
expect_fail('카탈로그에 없는 필드키', "INSERT INTO field_value (doc_id,kind_id,field_key,fill_state,value_text,author_id,updated_at) VALUES ('DOC-REQ-HWCHECK','D01','foo','filled','x','u-jjm','t')")

x("INSERT INTO item (item_id,project_id,item_type,title,defined_in,created_at,updated_at) VALUES ('REQ-HW-001','PRJ-ITEASY','requirement','디스크 사용률 점검','DOC-REQ-HWCHECK',?,?)", (NOW, NOW))
x("INSERT INTO item (item_id,project_id,item_type,title,attrs,created_at,updated_at) VALUES ('RES-DB01','PRJ-ITEASY','infra_resource','DB 서버 1호기','{\"kind\":\"server\",\"env\":\"ENV-PROD\"}',?,?)", (NOW, NOW))
x("INSERT INTO item (item_id,project_id,item_type,title,created_at,updated_at) VALUES ('TC-HW-001','PRJ-ITEASY','test_case','디스크 점검 테스트',?,?)", (NOW, NOW))
expect_fail('항목 ID 접두어 불일치', "INSERT INTO item (item_id,project_id,item_type,title,created_at,updated_at) VALUES ('X-1','PRJ-ITEASY','requirement','x','t','t')")
x("INSERT INTO relation (from_type,from_id,rel_type,to_type,to_id,is_required,created_at) VALUES ('item','REQ-HW-001','verified_by','item','TC-HW-001',1,?)", (NOW,))
rtm = x("SELECT requirement_id, verified_by FROM v_rtm").fetchall()
ok('D08 RTM = 조회로 생성', rtm == [('REQ-HW-001', 'TC-HW-001')], str(rtm))
expect_fail('AT-17 경로 탈출 ../', "INSERT INTO artifact (artifact_id,uri,role,source_mode) VALUES ('A1','../etc/passwd','code','authored')")
expect_fail('AT-17 절대경로', "INSERT INTO artifact (artifact_id,uri,role,source_mode) VALUES ('A2','/etc/passwd','code','authored')")

# ---- 실행 증거 (하드웨어 점검)
x("""INSERT INTO run (run_id,work_id,run_type,target_ref,env_item_id,runner,command_argv,result,trust_source,recorded_at)
     VALUES ('RUN-0001','WORK-2026-0001','hw_check','git:abc123','RES-DB01','ci-runner','["check_disk","--host","db01"]','PASS','ci',?)""", (NOW,))
expect_fail('AT-14 종료된 실행 결과 덮어쓰기', "UPDATE run SET result='FAIL' WHERE run_id='RUN-0001'")
expect_fail('결과 5종 외 값', "INSERT INTO run (run_id,run_type,target_ref,runner,result,trust_source,recorded_at) VALUES ('R2','test','x','r','SUCCESS','ci','t')")

# ---- 이력: 링크드 리스트 + 순번 + 해시 체인
def append(entity, after, actor='u-jjm'):
    last = x("SELECT change_id, seq, row_digest FROM change_log WHERE entity_type='field_value' AND entity_id=? ORDER BY seq DESC LIMIT 1", (entity,)).fetchone()
    seq, prev, pd = (1, None, None) if not last else (last[1] + 1, last[0], last[2])
    body = json.dumps({'e': entity, 'seq': seq, 'after': after, 'prev': pd}, sort_keys=True, ensure_ascii=False)
    dg = 'sha256:' + hashlib.sha256(body.encode()).hexdigest()
    x("INSERT INTO change_log (entity_type,entity_id,seq,prev_change_id,op,after_json,actor_id,changed_at,row_digest,prev_digest) VALUES ('field_value',?,?,?,?,?,?,?,?,?)",
      (entity, seq, prev, 'insert' if seq == 1 else 'update', json.dumps(after, ensure_ascii=False), actor, NOW, dg, pd))
    return dg
E = 'DOC-REQ-HWCHECK|D01|behavior'
for i in range(1, 4): last_dg = append(E, {'value_text': f'v{i}'})
hist = x("""WITH RECURSIVE h(id, seq, prev) AS (
             SELECT change_id, seq, prev_change_id FROM change_log c WHERE entity_id=? AND NOT EXISTS
               (SELECT 1 FROM change_log n WHERE n.prev_change_id = c.change_id)
             UNION ALL SELECT c.change_id, c.seq, c.prev_change_id FROM change_log c JOIN h ON c.change_id = h.prev)
           SELECT seq FROM h""", (E,)).fetchall()
ok('이력 역순 순회 (WITH RECURSIVE)', [s for (s,) in hist] == [3, 2, 1], str(hist))
ok('N번째 직접 조회 (seq 인덱스)', x("SELECT json_extract(after_json,'$.value_text') FROM change_log WHERE entity_id=? AND seq=2", (E,)).fetchone()[0] == 'v2')
expect_fail('분기(fork): 같은 prev를 두 번 가리킴', "INSERT INTO change_log (entity_type,entity_id,seq,prev_change_id,op,actor_id,changed_at,row_digest,prev_digest) SELECT 'field_value',?,3,change_id,'update','u-jjm','t','x',row_digest FROM change_log WHERE entity_id=? AND seq=2", (E, E))
expect_fail('해시 체인 불일치(위변조)', "INSERT INTO change_log (entity_type,entity_id,seq,prev_change_id,op,actor_id,changed_at,row_digest,prev_digest) SELECT 'field_value',?,4,change_id,'update','u-jjm','t','x','sha256:forged' FROM change_log WHERE entity_id=? AND seq=3", (E, E))
expect_fail('이력 수정 금지', "UPDATE change_log SET reason='x' WHERE seq=1")
expect_fail('이력 삭제 금지', "DELETE FROM change_log WHERE seq=1")

# ---- 리비전·승인
x("INSERT INTO doc_revision (doc_id,revision_no,prev_revision_id,snapshot_json,content_digest,reason,created_by,created_at) VALUES ('DOC-REQ-HWCHECK',1,NULL,'{}','sha256:r1','초안 기준선','u-jjm',?)", (NOW,))
expect_fail('AI 승인 금지', "INSERT INTO approval (approval_id,revision_id,approver_id,decision,decided_at) VALUES ('AP1',1,'ai-codex','approved','t')")
x("INSERT INTO approval (approval_id,revision_id,gate,approver_id,decision,decided_at) VALUES ('AP2',1,'design-approval','u-jjm','approved',?)", (NOW,))
ok('사람 승인 기록', x("SELECT count(*) FROM approval").fetchone()[0] == 1)

# ---- 보존
x("UPDATE document SET legal_hold=1 WHERE doc_id='DOC-REQ-HWCHECK'")
expect_fail('AT-23 legal_hold 문서 삭제', "DELETE FROM document WHERE doc_id='DOC-REQ-HWCHECK'")
x("INSERT INTO item (item_id,project_id,item_type,title,created_at,updated_at) VALUES ('ROLE-OPS','PRJ-ITEASY','role','운영 책임',?,?)", (NOW, NOW))
x("INSERT INTO role_assignment VALUES ('ROLE-OPS','u-jjm','A',?,NULL)", (NOW,))
expect_fail('B03 단일 최종 책임(A) 중복', "INSERT INTO role_assignment VALUES ('ROLE-OPS','team-ops','A','2026-10-09',NULL)")
# ---- AI 협업: 컨텍스트 잠금 · 최근 값 · 재개
head = x("SELECT change_id FROM v_entity_head WHERE entity_id=?", (E,)).fetchone()[0]
ok('최신 값 = 링크드 리스트 head', x("SELECT seq FROM change_log WHERE change_id=?", (head,)).fetchone()[0] == 3)
x("INSERT INTO context_read (work_id,actor_id,session_ref,target_type,target_id,last_change_id,reason,read_at) VALUES ('WORK-2026-0001','ai-codex','S-1','document','DOC-REQ-HWCHECK',?,'변경 대상 요구사항',?)", (head, NOW))
ok('읽은 뒤 변경 없음 → 낡은 근거 0', x("SELECT count(*) FROM v_context_stale").fetchone()[0] == 0)
append(E, {'value_text': 'v4'})
st = x("SELECT target_id, read_change_id, latest_change_id FROM v_context_stale").fetchall()
ok('읽은 뒤 변경 → 낡은 근거 탐지', len(st) == 1, str(st))
x("""INSERT INTO handoff (handoff_id,work_id,from_role,to_role,payload,created_at) VALUES
     ('HO-0001','WORK-2026-0001','implementer','reviewer','{"next_actions":["디스크 임계값 검토"],"not_run":["부하 테스트"]}',?)""", (NOW,))
x("INSERT INTO run (run_id,work_id,run_type,target_ref,runner,result,trust_source,recorded_at) VALUES ('RUN-0002','WORK-2026-0001','load_test','git:abc123','manual','NOT_RUN','manual',?)", (NOW,))
card = x("SELECT last_handoff_id,last_handoff_ack,next_actions,unresolved_runs,stale_context FROM v_work_resume WHERE work_id='WORK-2026-0001'").fetchone()
ok('작업 재개 카드', card == ('HO-0001', 'pending', '["디스크 임계값 검토"]', 1, 1), str(card))
expect_fail('인계 수신 확인 시 기준 커밋 미확인', "UPDATE handoff SET ack_status='accepted', ack_by='u-jjm' WHERE handoff_id='HO-0001'")
rc = x("SELECT actor_type, entity_id, seq FROM v_recent_changes LIMIT 1").fetchone()
ok('최근 변경 조회 (사람/AI 구분)', rc == ('human', E, 4), str(rc))
expect_fail('컨텍스트 기록 수정 금지', "UPDATE context_read SET reason='x'")
ok('FK 무결성', con.execute('PRAGMA foreign_key_check').fetchall() == [])

w = max(len(n) for n, _ in R)
for n, r in R: print(n.ljust(w), '|', r)
print('TOTAL', len(R), 'FAIL', sum(1 for _, r in R if r.startswith('FAIL')))
con.commit()
