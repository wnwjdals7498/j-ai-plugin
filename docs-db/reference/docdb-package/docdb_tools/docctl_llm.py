"""docctl_llm — LLM(에이전트)이 DB를 읽고 쓰는 경로. 토큰 최소화가 목적.

명령 6개: ctx(작업 시작·재개 묶음) / get(핸들 조회) / find(검색) / trace(관계) / since(증분) / put(쓰기)
원칙
  - 응답은 한 줄 1항목의 압축 텍스트. 첫 줄에 커서(as_of=c:N)와 예산 사용량(tok=used/budget)
  - 예산을 넘으면 자르고 omitted=N 으로 알린다 (조용히 자르지 않음)
  - 본문 전문(L2)은 f: 핸들로 필드 하나씩만. 문서 전체를 한 번에 주지 않음
  - 읽을 때 받은 dg(digest)를 다시 주면 바뀌지 않은 값은 'UNCHANGED' 한 줄
  - 쓰기 응답은 내용을 되돌려주지 않음 (OK/ERR + 새 dg)
핸들: d:문서  f:문서/종류/필드  i:항목  w:작업  r:실행  k:문서종류
"""
import sys, json, math, hashlib, sqlite3, datetime, difflib, argparse

SNIP = 80            # L1 요약이 없을 때 보여 줄 앞부분 글자 수
SEV = {'critical': 0, 'error': 1, 'warning': 2, 'info': 3}

# ---- 토큰 추정: 보정 전 휴리스틱 (과대추정 쪽). count_tokens API로 계수 보정 필요 ----
ASCII_PER_TOK, NONASCII_PER_TOK = 3.5, 1.0
def tok_est(s):
    if not s:
        return 0
    a = sum(1 for ch in s if ord(ch) < 128)
    return math.ceil(a / ASCII_PER_TOK + (len(s) - a) / NONASCII_PER_TOK)

def digest(s):
    return hashlib.sha256((s or '').encode()).hexdigest()[:16]

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')

def q(s):
    s = str(s).replace('\n', ' ⏎ ')
    return f'"{s}"' if (s == '' or ' ' in s) else s

def fh(doc, kind, field):
    return f'f:{doc}/{kind}/{field}'

def parse(h):
    t, _, rest = h.partition(':')
    if t == 'f':
        doc, kind, field = rest.split('/')
        return 'f', (doc, kind, field)
    return t, rest

ENTITY = {'d': 'document', 'i': 'item', 'w': 'work', 'r': 'run', 'a': 'artifact'}
PREFIX = {v: k for k, v in ENTITY.items()}


class Out:
    """예산 안에서만 줄을 담는다. force=True 줄(작업 카드·차단 요인)은 예산과 무관하게 넣는다."""
    def __init__(self, budget):
        self.budget, self.lines, self.used, self.omitted = budget, [], 0, 0

    def add(self, line, force=False):
        t = tok_est(line) + 1
        if not force and self.budget and self.used + t > self.budget:
            self.omitted += 1
            return False
        self.lines.append(line)
        self.used += t
        return True


class DocDB:
    def __init__(self, path_or_con, actor='ai-agent', work=None, session=None):
        self.con = path_or_con if isinstance(path_or_con, sqlite3.Connection) else sqlite3.connect(path_or_con)
        self.con.execute('PRAGMA foreign_keys=ON')
        self.actor, self.work, self.session = actor, work, session
        self._seen = set()

    def x(self, sql, a=()):
        return self.con.execute(sql, a)

    def cursor(self):
        return self.x('SELECT coalesce(max(change_id),0) FROM change_log').fetchone()[0]

    # ---------- 공통 ----------
    def _finish(self, tool, args, out, header):
        head = f'#{tool} {header} as_of=c:{self.cursor()} tok={out.used}/{out.budget or "-"}'
        if out.omitted:
            head += f' omitted={out.omitted}'
        text = '\n'.join([head] + out.lines)
        self.x('INSERT INTO call_log (tool,args_json,actor_id,work_id,out_chars,out_tok_est,budget,omitted,called_at)'
               ' VALUES (?,?,?,?,?,?,?,?,?)',
               (tool, json.dumps(args, ensure_ascii=False), self.actor, self.work, len(text), tok_est(text),
                out.budget, out.omitted, now()))
        self.con.commit()
        self._seen.clear()
        return text

    def _read(self, ttype, tid):
        """V05 컨텍스트 잠금 자동 기록: 내용(L1 이상)을 실었을 때만"""
        if not self.work or (ttype, tid) in self._seen:
            return
        self._seen.add((ttype, tid))
        self.x('INSERT INTO context_read (work_id,actor_id,session_ref,target_type,target_id,last_change_id,reason,read_at)'
               ' VALUES (?,?,?,?,?,?,?,?)',
               (self.work, self.actor, self.session, ttype, tid, self.cursor() or None, 'auto', now()))

    def _field_row(self, doc, kind, field):
        return self.x('SELECT fill_state, format, value_text, value_json, ref_locator, reason, digest, tok_est'
                      ' FROM field_value WHERE doc_id=? AND kind_id=? AND field_key=?', (doc, kind, field)).fetchone()

    def _field_line(self, doc, kind, field, level, row=None, head=True):
        """head=False: 최신 요약이 없으면 앞부분 발췌 대신 L0 한 줄만 (ctx 에서 사용)"""
        row = row or self._field_row(doc, kind, field)
        h = fh(doc, kind, field)
        if row is None:
            return f'{h} MISSING'
        st, fmt, vt, vj, loc, reason, d, te = row
        if st != 'filled':
            return f'{h} {st} reason={q(reason)}'
        body = vt if fmt == 'md' else (vj if fmt == 'json' else f'ref {loc}')
        meta = f'dg={d} tok={te}'
        if level == 0:
            return f'{h} {meta}'
        if level == 1:
            s = self.x('SELECT summary, source_digest FROM entity_summary WHERE handle=?', (h,)).fetchone()
            if s and s[1] == d:
                return f'{h} {meta} sum={q(s[0])}'
            if not head:
                return f'{h} {meta} {"sum=stale" if s else "sum=none"}'
            head = body[:SNIP]
            more = len(body) - len(head)
            return f'{h} {meta} {"sum=stale" if s else "sum=none"} head={q(head)}' + (f' +{more}ch' if more > 0 else '')
        return f'{h} {meta}\n{body}'

    def _doc_line(self, doc):
        r = self.x('SELECT line FROM v_l0_document WHERE doc_id=?', (doc,)).fetchone()
        miss = [m[0] for m in self.x('SELECT field_key FROM v_missing_required_fields WHERE doc_id=?', (doc,))]
        return (r[0] if r else f'D {doc} NOT_FOUND') + (f' missing={",".join(miss)}' if miss else '')

    def _doc_fields(self, doc):
        return self.x('SELECT dk.kind_id, f.field_key FROM document_kind dk'
                      ' JOIN field_value v ON v.doc_id = dk.doc_id AND v.kind_id = dk.kind_id'
                      ' JOIN cat_kind_field f ON f.kind_id = v.kind_id AND f.field_key = v.field_key'
                      ' WHERE dk.doc_id=? ORDER BY dk.kind_id, f.sort_order', (doc,)).fetchall()

    def _item_line(self, iid, level):
        r = self.x('SELECT item_type, status, title, attrs, digest FROM item WHERE item_id=?', (iid,)).fetchone()
        if not r:
            return f'i:{iid} NOT_FOUND'
        line = f'i:{iid} {r[0]} {r[1] or "-"} {q(r[2])}'
        if level >= 1 and r[3] not in (None, '{}'):
            line += f' attrs={r[3]}'
        return line

    # ---------- get ----------
    def get(self, handles, level=1, known=None, budget=2000):
        """level 0: 한 줄 / 1: 요약 / 2: 전문(f: 핸들만). known={handle: dg} → 바뀌지 않았으면 UNCHANGED"""
        out, known = Out(budget), known or {}
        for h in handles:
            t, key = parse(h)
            if t == 'f':
                doc, kind, field = key
                row = self._field_row(doc, kind, field)
                if row and known.get(h) == row[6]:
                    out.add(f'{h} UNCHANGED')
                    continue
                if out.add(self._field_line(doc, kind, field, level, row)) and level >= 1:
                    self._read('document', doc)
            elif t == 'd':
                out.add(self._doc_line(key))
                if level >= 1:   # 문서 핸들은 필드 목록까지만. 전문은 f: 로 하나씩
                    for kind, field in self._doc_fields(key):
                        out.add(self._field_line(key, kind, field, level - 1))
                    self._read('document', key)
            elif t == 'i':
                if out.add(self._item_line(key, level)) and level >= 1:
                    self._read('item', key)
            elif t == 'k':
                for fk, req, desc in self.x('SELECT field_key, is_required, description FROM cat_kind_field'
                                            ' WHERE kind_id=? ORDER BY sort_order', (key,)):
                    out.add(f'{"*" if req else "-"}{fk}' + (f' {q(desc)}' if level >= 1 else ''))
            else:
                out.add(f'{h} UNSUPPORTED')
        return self._finish('get', {'h': handles, 'level': level}, out, f'level={level}')

    # ---------- find ----------
    def find(self, query, limit=10, budget=800):
        out, terms = Out(budget), query.split()
        if all(len(t) >= 3 for t in terms):
            mode = 'fts'
            rows = self.x("SELECT rowid, snippet(fts_text, 0, '[', ']', '…', 8) FROM fts_text"
                          " WHERE fts_text MATCH ? ORDER BY bm25(fts_text) LIMIT ?",
                          (' AND '.join(f'"{t}"' for t in terms), limit)).fetchall()
        else:  # trigram은 3글자 미만 검색어를 MATCH하지 못함 → LIKE (선형 탐색)
            mode = 'like'
            cond = ' AND '.join(['body LIKE ?'] * len(terms))
            rows = self.x(f'SELECT unit_id, substr(body, max(1, instr(body, ?) - 20), 50) FROM v_text_unit'
                          f' WHERE {cond} LIMIT ?', (terms[0], *[f'%{t}%' for t in terms], limit)).fetchall()
        for uid, snip in rows:
            u = self.x('SELECT src, doc_id, kind_id, field_key, item_id FROM text_unit WHERE unit_id=?', (uid,)).fetchone()
            h = fh(u[1], u[2], u[3]) if u[0] == 'f' else f'i:{u[4]}'
            out.add(f'{h} {q(snip)}')
        return self._finish('find', {'q': query, 'limit': limit}, out, f'q={q(query)} mode={mode} hits={len(rows)}')

    # ---------- trace ----------
    def trace(self, handle, rel_types=None, depth=1, budget=800):
        """관계는 ID만. 내용이 필요하면 get"""
        out = Out(budget)
        t, key = parse(handle)
        frontier, seen = [(ENTITY.get(t, t), key)], set()
        for _ in range(depth):
            nxt = []
            for et, eid in frontier:
                if (et, eid) in seen:
                    continue
                seen.add((et, eid))
                sql = ('SELECT from_type, from_id, rel_type, to_type, to_id FROM relation'
                       ' WHERE ((from_type=? AND from_id=?) OR (to_type=? AND to_id=?))')
                args = [et, eid, et, eid]
                if rel_types:
                    sql += f' AND rel_type IN ({",".join("?" * len(rel_types))})'
                    args += rel_types
                for ft, fi, rt, tt, ti in self.x(sql + ' ORDER BY rel_id', args):
                    out.add(f'{PREFIX.get(ft, ft)}:{fi} -{rt}-> {PREFIX.get(tt, tt)}:{ti}')
                    nxt.append((tt, ti) if (ft, fi) == (et, eid) else (ft, fi))
            frontier = nxt
        return self._finish('trace', {'h': handle, 'rel': rel_types, 'depth': depth}, out, f'{handle} depth={depth}')

    # ---------- since ----------
    def since(self, cursor, work=None, budget=1200, diff_lines=6):
        """커서 이후 바뀐 것만. 엔터티별로 묶고 텍스트는 짧은 diff"""
        out = Out(budget)
        sql = ('SELECT entity_type, entity_id, max(change_id), count(*), group_concat(DISTINCT actor_id)'
               ' FROM change_log WHERE change_id > ?')
        args = [cursor]
        if work:
            sql += ' AND work_id = ?'
            args.append(work)
        rows = self.x(sql + ' GROUP BY entity_type, entity_id ORDER BY max(change_id)', args).fetchall()
        for et, eid, last, n, actors in rows:
            h = fh(*eid.split('|')) if et == 'field_value' else f'{PREFIX.get(et, et)}:{eid}'
            after = self.x('SELECT after_json FROM change_log WHERE change_id=?', (last,)).fetchone()[0]
            before = self.x('SELECT after_json FROM change_log WHERE entity_type=? AND entity_id=? AND change_id<=?'
                            ' ORDER BY change_id DESC LIMIT 1', (et, eid, cursor)).fetchone()
            a = json.loads(after or 'null')
            b = json.loads(before[0] or 'null') if before else None
            line = f'{h} x{n} by={actors} c:{last}'
            if isinstance(a, dict) and 'value_text' in a:
                old, new = (b or {}).get('value_text') or '', a.get('value_text') or ''
                diff = [l for l in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm='', n=0)
                        if l[:1] in '+-' and l[:3] not in ('+++', '---')]
                line += f' {len(old)}→{len(new)}ch'
                if out.add(line):
                    for l in diff[:diff_lines]:
                        out.add('  ' + l[:120])
                    if len(diff) > diff_lines:
                        out.add(f'  … +{len(diff) - diff_lines} lines (get {h} level=2)')
            else:
                out.add(line + f' new={q(json.dumps(a, ensure_ascii=False)[:120])}')
        return self._finish('since', {'cursor': cursor, 'work': work}, out, f'from=c:{cursor} entities={len(rows)}')

    # ---------- ctx ----------
    def ctx(self, work_id, budget=1500):
        """작업 시작·재개 묶음. 우선순위: 카드 > 차단 요인 > 관련 문서(L0) > 항목 종류별 개수 > 필드(L1) > 항목(L0) > 생략 수"""
        self.work = work_id
        out = Out(budget)
        c = self.x('SELECT title,status,owner_id,last_handoff_id,last_handoff_ack,next_actions,open_findings,'
                   'unresolved_runs,stale_context FROM v_work_resume WHERE work_id=?', (work_id,)).fetchone()
        if not c:
            out.add(f'w:{work_id} NOT_FOUND_OR_CLOSED', force=True)
            return self._finish('ctx', {'w': work_id}, out, f'w:{work_id}')
        out.add(f'W {work_id} {c[1]} owner={c[2]} {q(c[0])} handoff={c[3] or "-"}:{c[4] or "-"}', force=True)
        for a in json.loads(c[5] or '[]'):
            out.add(f'> {a}', force=True)
        # 차단 요인
        for rid, rt, res in self.x("SELECT run_id, run_type, result FROM run WHERE work_id=?"
                                   " AND result IN ('FAIL','NOT_RUN','ERROR','UNKNOWN') ORDER BY recorded_at", (work_id,)):
            out.add(f'! r:{rid} {rt} {res}', force=True)
        fs = self.x("SELECT f.finding_id, f.severity, f.code, f.target_type, f.target_id, f.message FROM finding f"
                    " JOIN run r ON r.run_id = f.run_id WHERE r.work_id=? AND f.disposition='open'", (work_id,)).fetchall()
        for fid, sev, code, tt, ti, msg in sorted(fs, key=lambda r: (SEV[r[1]], r[0])):
            out.add(f'! F{fid} {sev} {code} {tt}:{ti} {q(msg[:80])}')
        for tt, ti, rc, lc in self.x('SELECT target_type, target_id, read_change_id, latest_change_id'
                                     ' FROM v_context_stale WHERE work_id=?', (work_id,)):
            out.add(f'! stale {PREFIX.get(tt, tt)}:{ti} read=c:{rc} now=c:{lc}')
        # 관련 문서: 작업 소속 · 작업이 바꾼 문서 · 이 작업에서 읽었던 문서
        docs = [r[0] for r in self.x(
            "SELECT doc_id FROM document WHERE work_id=?1 OR origin_work_id=?1"
            " UNION SELECT to_id FROM relation WHERE from_type='work' AND from_id=?1 AND to_type='document'"
            " UNION SELECT target_id FROM context_read WHERE work_id=?1 AND target_type='document'"
            " ORDER BY 1", (work_id,))]
        for d in docs:
            out.add(self._doc_line(d))
        ph = ','.join('?' * len(docs))
        items = self.x(
            f"SELECT item_type, item_id FROM item WHERE defined_in IN ({ph})"
            f" UNION SELECT i.item_type, i.item_id FROM doc_scope s JOIN item i ON i.item_id = s.item_id"
            f" WHERE s.doc_id IN ({ph}) ORDER BY 1, 2", docs + docs).fetchall() if docs else []
        by_type = {}
        for it, iid in items:
            by_type.setdefault(it, []).append(iid)
        for it, ids in by_type.items():   # 항목은 종류별 개수·범위로 먼저 요약 (싸고 정보량이 큼)
            out.add(f'I {it}×{len(ids)} {ids[0]}…{ids[-1]}' if len(ids) > 1 else f'I {it} {ids[0]}')
        for d in docs:                    # 필드: 최신 요약이 있으면 요약, 없으면 핸들·dg·tok 한 줄
            for kind, field in self._doc_fields(d):
                if out.add(self._field_line(d, kind, field, 1, head=False)):
                    self._read('document', d)
        for it, iid in items:             # 개별 항목 줄은 예산이 남을 때만
            out.add(self._item_line(iid, 0))
        if out.omitted:
            out.add('… omitted → get <handle> / since <cursor>', force=True)
        return self._finish('ctx', {'w': work_id, 'budget': budget}, out, f'w:{work_id} docs={len(docs)} items={len(items)}')

    # ---------- put ----------
    def put(self, patches, reason=None):
        """patches: [{h, op: set|append|replace_section, text, expect(dg), state, why, anchor}]
        전부 성공하거나 전부 취소(원자적). 응답에 본문을 되돌려주지 않는다."""
        out, res, errors = Out(None), [], 0
        self.x('SAVEPOINT put')
        try:
            for p in patches:
                h = p['h']
                t, key = parse(h)
                if t != 'f':
                    res.append(f'ERR {h} UNSUPPORTED_HANDLE'); errors += 1; continue
                doc, kind, field = key
                row = self._field_row(doc, kind, field)
                cur_text, cur_dg = (row[2] if row else None), (row[6] if row else None)
                if 'expect' in p and p['expect'] != cur_dg:
                    res.append(f'ERR {h} CONFLICT now={cur_dg} (since 로 변경분 확인 후 재시도)'); errors += 1; continue
                op, state = p.get('op', 'set'), p.get('state', 'filled')
                if state != 'filled':
                    new = None
                elif op == 'set':
                    new = p['text']
                elif op == 'append':
                    new = (cur_text + '\n' if cur_text else '') + p['text']
                elif op == 'replace_section':
                    new = replace_section(cur_text or '', p['anchor'], p['text'])
                    if new is None:
                        res.append(f'ERR {h} ANCHOR_NOT_FOUND {q(p["anchor"])}'); errors += 1; continue
                else:
                    res.append(f'ERR {h} BAD_OP {op}'); errors += 1; continue
                d, te = digest(new), tok_est(new)
                try:
                    self.x('INSERT INTO field_value (doc_id,kind_id,field_key,fill_state,value_text,reason,author_id,'
                           'updated_at,digest,tok_est) VALUES (?,?,?,?,?,?,?,?,?,?)'
                           ' ON CONFLICT(doc_id,kind_id,field_key) DO UPDATE SET fill_state=excluded.fill_state,'
                           ' value_text=excluded.value_text, reason=excluded.reason, author_id=excluded.author_id,'
                           ' updated_at=excluded.updated_at, digest=excluded.digest, tok_est=excluded.tok_est',
                           (doc, kind, field, state, new, p.get('why'), self.actor, now(), d, te))
                    self._append_change('field_value', f'{doc}|{kind}|{field}',
                                        {'value_text': cur_text} if row else None,
                                        {'value_text': new, 'fill_state': state}, reason or p.get('why'))
                except sqlite3.DatabaseError as e:
                    res.append(f'ERR {h} DB {q(str(e)[:80])}'); errors += 1; continue
                res.append(f'OK {h} dg={d} tok={te}')
            if errors:
                self.x('ROLLBACK TO put')
        finally:
            self.x('RELEASE put')
        out.add(f'{"APPLIED" if not errors else "NOTHING_APPLIED"} ok={len(patches) - errors} err={errors}')
        for r in res:
            out.add(r)
        return self._finish('put', {'n': len(patches)}, out, f'n={len(patches)}')

    def _append_change(self, et, eid, before, after, reason):
        last = self.x('SELECT change_id, seq, row_digest FROM change_log WHERE entity_type=? AND entity_id=?'
                      ' ORDER BY seq DESC LIMIT 1', (et, eid)).fetchone()
        seq, prev, pd = (1, None, None) if not last else (last[1] + 1, last[0], last[2])
        body = json.dumps({'e': eid, 'seq': seq, 'after': after, 'prev': pd}, sort_keys=True, ensure_ascii=False)
        rd = 'sha256:' + hashlib.sha256(body.encode()).hexdigest()
        self.x('INSERT INTO change_log (entity_type,entity_id,seq,prev_change_id,op,before_json,after_json,actor_id,'
               'work_id,reason,changed_at,row_digest,prev_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (et, eid, seq, prev, 'insert' if seq == 1 else 'update',
                json.dumps(before, ensure_ascii=False) if before is not None else None,
                json.dumps(after, ensure_ascii=False), self.actor, self.work, reason, now(), rd, pd))


def replace_section(text, anchor, new_body):
    """md 제목 줄(anchor)부터 같은 수준 이상의 다음 제목 전까지를 교체. 없으면 None"""
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if l.strip() == anchor.strip():
            level = len(l) - len(l.lstrip('#'))
            j = i + 1
            while j < len(lines):
                s = lines[j]
                lv = len(s) - len(s.lstrip('#'))
                if lv and lv <= level and s[lv:lv + 1] == ' ':
                    break
                j += 1
            return '\n'.join(lines[:i + 1] + new_body.splitlines() + lines[j:])
    return None


def main():
    ap = argparse.ArgumentParser(prog='docctl')
    ap.add_argument('--db', required=True)
    ap.add_argument('--actor', default='ai-agent')
    ap.add_argument('--work')
    sp = ap.add_subparsers(dest='cmd', required=True)
    a = sp.add_parser('ctx'); a.add_argument('work_id'); a.add_argument('--budget', type=int, default=1500)
    a = sp.add_parser('get'); a.add_argument('handles', nargs='+'); a.add_argument('--level', type=int, default=1)
    a.add_argument('--known', default='{}', help='{"handle":"dg"} JSON'); a.add_argument('--budget', type=int, default=2000)
    a = sp.add_parser('find'); a.add_argument('query'); a.add_argument('--limit', type=int, default=10)
    a = sp.add_parser('trace'); a.add_argument('handle'); a.add_argument('--rel', nargs='*'); a.add_argument('--depth', type=int, default=1)
    a = sp.add_parser('since'); a.add_argument('cursor', type=int); a.add_argument('--work-only', action='store_true')
    a = sp.add_parser('put'); a.add_argument('patches', help='JSON 배열 또는 @파일')
    ns = ap.parse_args()
    db = DocDB(ns.db, ns.actor, ns.work)
    if ns.cmd == 'ctx':
        print(db.ctx(ns.work_id, ns.budget))
    elif ns.cmd == 'get':
        print(db.get(ns.handles, ns.level, json.loads(ns.known), ns.budget))
    elif ns.cmd == 'find':
        print(db.find(ns.query, ns.limit))
    elif ns.cmd == 'trace':
        print(db.trace(ns.handle, ns.rel, ns.depth))
    elif ns.cmd == 'since':
        print(db.since(ns.cursor, ns.work if ns.work_only else None))
    elif ns.cmd == 'put':
        raw = open(ns.patches[1:], encoding='utf-8').read() if ns.patches.startswith('@') else ns.patches
        print(db.put(json.loads(raw)))


if __name__ == '__main__':
    main()
