"""catalogue.yaml -> SQLite cat_* 테이블 적재 (검수용)"""
import sys, json, hashlib, sqlite3, datetime, yaml

VIEW = {'A01', 'D08', 'F06', 'A03'}  # 조회로 생성 (작성 안 함). A03=cat_* 정의에서 필요 시 생성, F06=relation 조회
AUTO = {'V05'}                        # 스크립트 자동 기록 (context_read)
RUN_FILES = ('evidence.json', 'results.json', 'run.json', 'report.json', 'findings.json')

def storage_class(k):
    files = k['content_files']
    if k['kind_id'] in VIEW:
        return 'view'
    if k['kind_id'] in AUTO:
        return 'run'
    if k['lifecycle'] == 'work' and any(f in RUN_FILES for f in files):
        return 'run'
    if 'detail.md' not in files:
        return 'register'
    return 'narrative'

def load(db, cat_path, schema_path):
    raw = open(cat_path, 'rb').read()
    d = yaml.safe_load(raw)
    con = sqlite3.connect(db)
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(open(schema_path, encoding='utf-8').read())
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    con.execute('INSERT INTO cat_version VALUES (?,?,?,?)',
                (d['standard_version'], d['schema_version'], 'sha256:' + hashlib.sha256(raw).hexdigest(), now))
    for code, g in d['groups'].items():
        con.execute('INSERT INTO cat_group VALUES (?,?,?,?,?,?)',
                    (code, g['directory'], g['title_ko'], g['title_en'], g['default_owner_role'], g['review_days']))
    for ftype in ('cap', 'evt'):
        for key, desc in d['facts'][ftype].items():
            rule = None
            if key == 'integration':
                rule = json.dumps({'any': [{'fact': 'cap.api'}, {'fact': 'cap.events'}, {'fact': 'cap.external_integration'}]})
            con.execute('INSERT INTO cat_fact VALUES (?,?,?,?)', (f'{ftype}.{key}', ftype, desc, rule))
    kinds = d['document_types']
    for k in kinds:
        con.execute('''INSERT INTO cat_kind (kind_id, abbreviation, title_ko, title_en, group_code, lifecycle, mode,
                       storage_class, owner_role, review_days, trigger_text, required_when, applies_when, content_files)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (k['kind_id'], k['abbreviation'], k['title_ko'], k['title_en'], k['category'], k['lifecycle'],
                     k['mode'], storage_class(k), k['owner_role'], k['review_days'], k['trigger'],
                     json.dumps(k['required_when'], ensure_ascii=False), json.dumps(k['applies_when'], ensure_ascii=False),
                     json.dumps(k['content_files'])))
    for k in kinds:
        keep = set(k['brief_must_preserve_keys'])
        i = 0
        for req, fields in ((1, k['required_fields']), (0, k['optional_fields'])):
            for fk, desc in fields.items():
                i += 1
                con.execute('INSERT INTO cat_kind_field VALUES (?,?,?,?,?,?)',
                            (k['kind_id'], fk, req, desc, int(fk in keep), i))
        for req, links in ((1, k['required_link_kinds']), (0, k['optional_link_kinds'])):
            for t in links:
                con.execute('INSERT INTO cat_kind_link VALUES (?,?,?)', (k['kind_id'], t, req))
        for n in k['covered_names']:
            con.execute('INSERT INTO cat_kind_alias (kind_id, name) VALUES (?,?)', (k['kind_id'], n))
    import os
    seed=os.path.join(os.path.dirname(schema_path),'docdb_seed_item_types.sql')
    if os.path.exists(seed): con.executescript(open(seed,encoding='utf-8').read())
    con.commit()
    return con, d

if __name__ == '__main__':
    con, _ = load(sys.argv[1], sys.argv[2], sys.argv[3])
    for t in ('cat_group', 'cat_fact', 'cat_kind', 'cat_kind_field', 'cat_kind_link', 'cat_kind_alias'):
        print(t, con.execute(f'SELECT count(*) FROM {t}').fetchone()[0])
    print(con.execute('SELECT storage_class, count(*) FROM cat_kind GROUP BY 1').fetchall())
    print('FK violations:', con.execute('PRAGMA foreign_key_check').fetchall()[:5])
