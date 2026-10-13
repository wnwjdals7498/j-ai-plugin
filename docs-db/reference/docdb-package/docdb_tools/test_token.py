"""토큰 절감 설계 동작·효과 검증
사용: python3 test_token.py <out.db> [catalogue.yaml] [docdb_schema.sql]
  catalogue를 주지 않으면 검증용 미니 카탈로그를 만들어 씀 (실제 194종 대신 필요한 종류만)
측정값은 글자 수와 tok_est(보정 전 휴리스틱)이며, 정확한 토큰 수는 count_tokens API로 다시 재야 함.
"""
import os, sys, json, random, tempfile, sqlite3
import yaml
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from load_catalogue import load
from docctl_llm import DocDB, tok_est, digest

out_db = sys.argv[1]
schema = sys.argv[3] if len(sys.argv) > 3 else os.path.join(HERE, '..', 'docdb_schema.sql')
ext = os.path.join(os.path.dirname(schema), 'docdb_schema_token_ext.sql')

# ---------------- 미니 카탈로그 ----------------
SEED_KINDS = ['A04', 'B02', 'B03', 'B04', 'C04', 'J08', 'D01', 'D03', 'D04', 'D05', 'D06', 'E01', 'E04', 'E05', 'E06',
              'E08', 'F02', 'F03', 'F07', 'H01', 'H05', 'J01', 'K01', 'K02', 'S03', 'L02', 'L07', 'M09', 'O06', 'P01',
              'P03', 'P04', 'P07', 'Q03', 'T07', 'U04', 'V01', 'I01', 'F04', 'P06', 'C06', 'K07', 'S04', 'G01']
def mini_catalogue(path):
    groups = {c: {'directory': f'{c.lower()}-area', 'title_ko': f'{c} 영역', 'title_en': f'{c} area',
                  'default_owner_role': 'owner', 'review_days': 90} for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'}
    rule = {'I01': {'fact': 'cap.database'}, 'E01': {'fact': 'cap.ui'}, 'F04': {'fact': 'cap.production'}}
    fields = {'D01': ['requirement_ids', 'behavior', 'acceptance', 'scope', 'owners'],
              'P06': ['steps', 'rollback', 'owners'], 'K02': ['resources', 'topology', 'owners']}
    kinds = []
    for k in SEED_KINDS:
        kinds.append({'kind_id': k, 'abbreviation': f'K-{k}', 'title_ko': f'{k} 문서', 'title_en': f'{k} doc',
                      'category': k[0], 'lifecycle': 'living', 'mode': 'mandatory', 'owner_role': 'owner',
                      'review_days': 90, 'trigger': None, 'required_when': rule.get(k, {'always': True}),
                      'applies_when': {'always': True}, 'content_files': ['meta.yaml', 'detail.md'],
                      'brief_must_preserve_keys': [],
                      'required_fields': {f: f'{f} 설명' for f in fields.get(k, ['overview', 'details', 'owners'])},
                      'optional_fields': {'notes': '비고'}, 'required_link_kinds': [], 'optional_link_kinds': [],
                      'covered_names': [f'{k} 이름']})
    kinds.append({**kinds[0], 'kind_id': 'F05', 'abbreviation': 'K-F05', 'category': 'F', 'lifecycle': 'work',
                  'content_files': ['run.json'], 'required_fields': {'execution': '실행'}, 'covered_names': []})
    cat = {'standard_version': 'mini-1', 'schema_version': '1', 'groups': groups,
           'facts': {'cap': {'database': 'DB', 'ui': 'UI', 'production': '운영'}, 'evt': {'release': '릴리스'}},
           'document_types': kinds}
    yaml.safe_dump(cat, open(path, 'w', encoding='utf-8'), allow_unicode=True)
    return path

cat = sys.argv[2] if len(sys.argv) > 2 else mini_catalogue(os.path.join(tempfile.mkdtemp(), 'mini-catalogue.yaml'))
if os.path.exists(out_db):
    os.remove(out_db)
con, d = load(out_db, cat, schema)
con.executescript(open(ext, encoding='utf-8').read())

# ---------------- 합성 데이터 ----------------
rnd = random.Random(42)
S = ['디스크 사용률이 임계값을 넘으면 경고를 보낸다.', '점검 결과는 실행 기록으로 남기고 문서에 복사하지 않는다.',
     '운영 서버는 매일 새벽 3시에 백업을 확인한다.', '장애가 발생하면 담당자는 15분 안에 상황을 공유한다.',
     '설정 변경은 Git 커밋과 함께 기록한다.', '검증되지 않은 값은 TBD로 표시하고 사유를 적는다.',
     '하드웨어 교체 후에는 인벤토리를 갱신한다.', '롤백 절차는 배포 전에 리허설한다.',
     '모니터링 대시보드에서 응답 시간을 확인한다.', '접근 권한은 분기마다 재검토한다.']
def body(n_sec=3):
    parts = ['## 개요', ' '.join(rnd.choice(S) for _ in range(4))]
    for i in range(1, n_sec + 1):
        parts += [f'### {i}단계', ' '.join(rnd.choice(S) for _ in range(rnd.randint(3, 7)))]
    parts += ['## 확인', ' '.join(rnd.choice(S) for _ in range(3))]
    return '\n'.join(parts)

x = con.execute
NOW = '2026-10-08T18:00:00+09:00'
x("INSERT INTO actor VALUES ('u-jjm','human','주정민',1),('ai-codex','ai','Codex',1),('ai-claude','ai','Claude',1)")
x("INSERT INTO project (project_id,name,catalogue_version) VALUES ('PRJ-T','검증',?)", (d['standard_version'],))
x("INSERT INTO work VALUES ('WORK-2026-0001','PRJ-T','feature','서버 점검 자동화','active','u-jjm','[]','[]',?,NULL)", (NOW,))
narr = [r[0] for r in x("SELECT kind_id FROM cat_kind WHERE lifecycle='living' AND storage_class='narrative' ORDER BY kind_id")]
pick = (['D01'] if 'D01' in narr else []) + [k for k in narr if k != 'D01'][:11]
human = DocDB(con, actor='u-jjm', work='WORK-2026-0001')
docs = []
for n, k in enumerate(pick):
    doc = f'DOC-{k}-{n:02d}'
    related = n < 4                                     # 앞 4개만 작업과 관련
    x("""INSERT INTO document (doc_id,project_id,title,lifecycle,category,authority,baseline_mode,owner_id,
         origin_work_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
      (doc, 'PRJ-T', f'{k} 문서 {n}', 'living', k[0], 'normative', 'desired', 'u-jjm',
       'WORK-2026-0001' if related else None, NOW, NOW))
    x("INSERT INTO document_kind VALUES (?,?)", (doc, k))
    fks = [r[0] for r in x("SELECT field_key FROM cat_kind_field WHERE kind_id=? ORDER BY sort_order", (k,))]
    patches = [{'h': f'f:{doc}/{k}/{f}', 'text': body(rnd.randint(2, 5))} for f in fks[:6]]
    if n == 0:                                          # 첫 문서는 필수 필드 하나를 일부러 비워 둠 (누락 탐지)
        patches = patches[:2] + patches[3:]
    r = human.put(patches)
    assert r.splitlines()[1].startswith('APPLIED'), r
    docs.append((doc, k, related))
# 항목·관계
D0 = docs[0][0]
for i in range(1, 41):
    x("INSERT INTO item (item_id,project_id,item_type,title,status,defined_in,attrs,created_at,updated_at)"
      " VALUES (?,?,?,?,?,?,?,?,?)", (f'REQ-HW-{i:03d}', 'PRJ-T', 'requirement', f'점검 요구 {i}: ' + rnd.choice(S),
                                     'approved', D0, json.dumps({'priority': rnd.choice(['P1', 'P2'])}), NOW, NOW))
    x("INSERT INTO item (item_id,project_id,item_type,title,created_at,updated_at) VALUES (?,?,?,?,?,?)",
      (f'TC-HW-{i:03d}', 'PRJ-T', 'test_case', f'테스트 {i}', NOW, NOW))
    x("INSERT INTO relation (from_type,from_id,rel_type,to_type,to_id,created_at) VALUES ('item',?,'verified_by','item',?,?)",
      (f'REQ-HW-{i:03d}', f'TC-HW-{i:03d}', NOW))
x("INSERT INTO run (run_id,work_id,run_type,target_ref,runner,result,trust_source,recorded_at) VALUES"
  " ('RUN-0001','WORK-2026-0001','hw_check','git:abc','ci','PASS','ci',?),"
  " ('RUN-0002','WORK-2026-0001','load_test','git:abc','manual','NOT_RUN','manual',?)", (NOW, NOW))
x("INSERT INTO finding (run_id,code,severity,target_type,target_id,message) VALUES"
  " ('RUN-0001','FIELD_MISSING','error','document',?,'필수 필드 누락'),"
  " ('RUN-0001','DRIFT','warning','item','REQ-HW-003','관측값이 기대값과 다름')", (D0,))
x("INSERT INTO handoff (handoff_id,work_id,from_role,to_role,payload,created_at) VALUES"
  " ('HO-0001','WORK-2026-0001','implementer','reviewer',?,?)",
  (json.dumps({'next_actions': ['디스크 임계값 검토', '부하 테스트 실행']}, ensure_ascii=False), NOW))
con.commit()

R = []
def ok(name, cond, detail=''):
    R.append((name, 'OK' if cond else 'FAIL', detail))
def size(s):
    return len(s), tok_est(s)

# ---------------- S1: 작업 시작 — 원시 덤프 vs ctx ----------------
rel_docs = [dd for dd, _, r in docs if r]
def rows(sql, a=()):
    cur = x(sql, a); cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
ph = ','.join('?' * len(rel_docs))
naive = json.dumps({
    'work': rows("SELECT * FROM work WHERE work_id='WORK-2026-0001'"),
    'documents': rows(f'SELECT * FROM document WHERE doc_id IN ({ph})', rel_docs),
    'fields': rows(f'SELECT * FROM field_value WHERE doc_id IN ({ph})', rel_docs),
    'items': rows(f'SELECT * FROM item WHERE defined_in IN ({ph})', rel_docs),
    'relations': rows('SELECT * FROM relation'),
    'runs': rows("SELECT * FROM run WHERE work_id='WORK-2026-0001'"),
    'findings': rows('SELECT * FROM finding'), 'handoffs': rows('SELECT * FROM handoff')}, ensure_ascii=False, indent=1)
agent = DocDB(con, actor='ai-codex', session='S-1')
ctx = agent.ctx('WORK-2026-0001', budget=1500)
head = ctx.splitlines()[0]
used = int(head.split('tok=')[1].split('/')[0])
ok('S1 ctx 예산 준수 (≤1500, 강제 줄 제외 오차 허용)', used <= 1500 + 100, head)
ok('S1 ctx에 차단 요인 포함 (NOT_RUN·open finding)', '! r:RUN-0002 load_test NOT_RUN' in ctx and '! F1 error FIELD_MISSING' in ctx)
ok('S1 ctx에 관련 문서만 (비관련 문서 제외)', all(f'D {dd} ' in ctx for dd in rel_docs) and
   not any(f'D {dd} ' in ctx for dd, _, r in docs if not r))
ok('S1 ctx 문서 줄에 누락 필수 필드 표시', any(l.startswith(f'D {D0} ') and 'missing=' in l for l in ctx.splitlines()),
   [l for l in ctx.splitlines() if l.startswith(f'D {D0} ')][0] if f'D {D0} ' in ctx else '')
ok('S1 항목은 종류별 개수로 먼저 요약', any(l.startswith('I requirement×40') for l in ctx.splitlines()))
n1, n2 = size(naive), size(ctx)
ok('S1 원시 덤프 대비 크기', n2[1] < n1[1], f'원시 {n1[0]:,}자/{n1[1]:,}tok → ctx {n2[0]:,}자/{n2[1]:,}tok ({n2[1]/n1[1]:.1%})')
reads = x("SELECT count(*) FROM context_read WHERE work_id='WORK-2026-0001' AND actor_id='ai-codex'").fetchone()[0]
ok('S1 컨텍스트 잠금 자동 기록 (context_read)', reads >= 1, f'{reads}건')

# ---------------- S2: 형식 — JSON 행 vs 한 줄 ----------------
items_json = json.dumps(rows("SELECT item_id,item_type,status,title FROM item WHERE item_type='requirement'"), ensure_ascii=False)
items_line = agent.get([f'i:REQ-HW-{i:03d}' for i in range(1, 41)], level=0, budget=None)
a1, a2 = size(items_json), size(items_line)
ok('S2 같은 40개 항목: JSON 대비 한 줄 형식', a2[1] < a1[1], f'JSON {a1[0]:,}자/{a1[1]:,}tok → 한 줄 {a2[0]:,}자/{a2[1]:,}tok ({a2[1]/a1[1]:.1%})')

# ---------------- S3: 다시 읽기 — 전문 vs UNCHANGED ----------------
H = f'f:{D0}/{docs[0][1]}/' + x("SELECT field_key FROM field_value WHERE doc_id=? ORDER BY rowid LIMIT 1", (D0,)).fetchone()[0]
full = agent.get([H], level=2)
dg0 = x("SELECT digest FROM field_value WHERE 'f:'||doc_id||'/'||kind_id||'/'||field_key=?", (H,)).fetchone()[0]
again = agent.get([H], level=2, known={H: dg0})
ok('S3 바뀌지 않은 값은 UNCHANGED 한 줄', again.splitlines()[1] == f'{H} UNCHANGED',
   f'전문 {size(full)[1]}tok → {size(again)[1]}tok')

# ---------------- S4: 쓰기 — 전체 재작성 vs 절 교체 ----------------
cur_text = x("SELECT value_text FROM field_value WHERE 'f:'||doc_id||'/'||kind_id||'/'||field_key=?", (H,)).fetchone()[0]
coder = DocDB(con, actor='ai-codex', work='WORK-2026-0001')
patch = {'h': H, 'op': 'replace_section', 'anchor': '### 1단계', 'text': '임계값은 85%로 한다. 초과 시 RUN 기록을 남긴다.', 'expect': dg0}
rewrite_cost = tok_est(cur_text.replace(cur_text.split('### 1단계')[1].split('###')[0], '\n' + patch['text'] + '\n'))
res = coder.put([patch], reason='임계값 확정')
new_text = x("SELECT value_text FROM field_value WHERE 'f:'||doc_id||'/'||kind_id||'/'||field_key=?", (H,)).fetchone()[0]
ok('S4 절 교체 적용 (다른 절 보존)', 'APPLIED' in res and '임계값은 85%' in new_text and '## 확인' in new_text and '### 2단계' in new_text)
ok('S4 LLM 출력량: 전체 재작성 vs 절 교체', True, f'재작성 {rewrite_cost}tok → 패치 {tok_est(json.dumps(patch, ensure_ascii=False))}tok')
ok('S4 쓰기 응답에 본문 없음', '임계값은 85%' not in res, res.replace('\n', ' | ')[:120])

# ---------------- S5: 충돌·원자성 ----------------
stale = coder.put([{'h': H, 'op': 'append', 'text': 'x', 'expect': dg0}])
ok('S5 낡은 dg로 쓰기 → CONFLICT, 적용 안 함', 'NOTHING_APPLIED' in stale and 'CONFLICT' in stale)
before = x("SELECT count(*) FROM change_log").fetchone()[0]
H2 = H.rsplit('/', 1)[0] + '/no_such_field'
mixed = coder.put([{'h': H, 'op': 'append', 'text': '추가 문장'}, {'h': H2, 'text': 'x'}])
ok('S5 배치 중 하나라도 실패 → 전부 취소', 'NOTHING_APPLIED' in mixed and x("SELECT count(*) FROM change_log").fetchone()[0] == before,
   mixed.splitlines()[-1][:100])

# ---------------- S6: 재개 — 전부 다시 읽기 vs since ----------------
cursor = x("SELECT max(change_id) FROM change_log").fetchone()[0]
for dd, k, r in docs[1:4]:
    f0 = x("SELECT field_key FROM field_value WHERE doc_id=? ORDER BY rowid LIMIT 1", (dd,)).fetchone()[0]
    coder.put([{'h': f'f:{dd}/{k}/{f0}', 'op': 'append', 'text': '변경: 점검 주기를 1시간으로 줄인다.'}])
reread = '\n'.join(agent.get([f'f:{dd}/{k}/{fk}' for dd, k, r in docs if r
                              for (fk,) in x("SELECT field_key FROM field_value WHERE doc_id=?", (dd,))], level=2, budget=None)
                   .splitlines())
delta = agent.since(cursor)
b1, b2 = size(reread), size(delta)
ok('S6 바뀐 3개만 반환', delta.splitlines()[0].count('entities=3') == 1, delta.splitlines()[0])
ok('S6 관련 문서 전체 재독 vs since', b2[1] < b1[1], f'재독 {b1[0]:,}자/{b1[1]:,}tok → since {b2[0]:,}자/{b2[1]:,}tok ({b2[1]/b1[1]:.1%})')

# ---------------- S7: 낡은 근거 → 다음 ctx에 표시 ----------------
ctx2 = agent.ctx('WORK-2026-0001', budget=1500)
ok('S7 읽은 뒤 바뀐 문서를 ! stale 로 표시', '! stale d:' in ctx2, [l for l in ctx2.splitlines() if l.startswith('! stale')][:2])

# ---------------- S8: 검색 (trigram + LIKE 대체) ----------------
f3 = agent.find('임계값을')
f2 = agent.find('장애')
ok('S8 3글자 이상 → FTS (조사 붙은 단어도 부분 일치)', 'mode=fts' in f3 and 'hits=0' not in f3, f3.splitlines()[0])
ok('S8 2글자 → LIKE 대체', 'mode=like' in f2 and 'hits=0' not in f2, f2.splitlines()[0])
raw_fts2 = x("SELECT count(*) FROM fts_text WHERE fts_text MATCH '\"장애\"'").fetchone()[0]
ok('S8 (근거) trigram MATCH는 2글자를 못 찾음', raw_fts2 == 0, f'MATCH "장애" = {raw_fts2}건')
coder.put([{'h': H, 'op': 'append', 'text': '신규어휘콴텀점검'}])
hit_new = x("SELECT count(*) FROM fts_text WHERE fts_text MATCH '\"콴텀점검\"'").fetchone()[0]
con.commit(); con.execute('VACUUM')
hit_after_vacuum = agent.find('콴텀점검')
ok('S8 수정 즉시 색인 반영 + VACUUM 후에도 핸들 유지', hit_new == 1 and H in hit_after_vacuum, hit_after_vacuum.splitlines()[-1][:90])
ok('S8 FTS 무결성 검사', x("INSERT INTO fts_text(fts_text) VALUES('integrity-check')") is not None)

# ---------------- S9: L1 요약 낡음 판정 ----------------
dg_now = x("SELECT digest FROM field_value WHERE 'f:'||doc_id||'/'||kind_id||'/'||field_key=?", (H,)).fetchone()[0]
x("INSERT INTO entity_summary VALUES (?,?,?,?,?,?,?)", (H, '임계값 85%, 초과 시 RUN 기록.', dg_now, 12, 'llm', 'ai-claude', NOW))
l1 = agent.get([H], level=1)
coder.put([{'h': H, 'op': 'append', 'text': '추가 변경.'}])
l1b = agent.get([H], level=1)
ok('S9 요약이 최신이면 sum=, 원본이 바뀌면 sum=stale', 'sum="임계값' in l1 and 'sum=stale' in l1b)
ok('S9 v_summary_stale 에 재요약 대상 표시', x("SELECT count(*) FROM v_summary_stale WHERE handle=?", (H,)).fetchone()[0] == 1)

# ---------------- S10: 관계 / 이력 체인 ----------------
tr = agent.trace('i:REQ-HW-001')
ok('S10 trace 는 ID만', tr.splitlines()[1] == 'i:REQ-HW-001 -verified_by-> i:TC-HW-001', tr.splitlines()[1])
ok('S10 put 이 쓴 change_log 체인 유효 (트리거 통과)', x("SELECT count(*) FROM change_log WHERE actor_id='ai-codex'").fetchone()[0] >= 5)
ok('S10 FK 무결성', x('PRAGMA foreign_key_check').fetchall() == [])

# ---------------- S11: 호출 로그 ----------------
top = x("SELECT tool, count(*), sum(out_tok_est), max(out_tok_est) FROM call_log GROUP BY tool ORDER BY 3 DESC").fetchall()
ok('S11 call_log 로 도구별 토큰 집계', len(top) >= 5, str(top))

w = max(len(n) for n, _, _ in R)
for n, s, dt in R:
    print(n.ljust(w), '|', s, '|', dt)
print('TOTAL', len(R), 'FAIL', sum(1 for _, s, _ in R if s == 'FAIL'))
print('\n--- ctx 출력 예 (앞 25줄) ---')
print('\n'.join(ctx.splitlines()[:25]))
print('\n--- since 출력 예 ---')
print(delta)
con.commit()
