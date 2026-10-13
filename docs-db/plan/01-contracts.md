# 01. 계약 (Contracts)

이 문서는 구현의 **유일한 이름·형식 원천**이다. 모듈 이름, 함수 서명, 인자, 반환 형식, 오류 코드, 환경 변수, CLI 명령, 출력 접두어, 알고리즘은 여기에 적힌 대로만 만든다. 여기에 없는 이름이 필요하면 만들지 말고 멈춰서 보고한다([00-discipline.md](00-discipline.md) 7절).

각 절은 `C-xx`로 참조한다. 근거 결정은 `D번호`([../09-decisions.md](../09-decisions.md))로 적었다.

---

## C-01 디렉터리와 모듈 (D1, D14)

플러그인 루트는 `j-ai-plugin/docdb/`다. 아래 트리 밖에 파일을 만들지 않는다.

```text
docdb/
  .claude-plugin/plugin.json        C-26
  .codex-plugin/plugin.json         C-26 (T00b 실측 후 확정)
  codex.mcp.json                    C-26 (T00b 실측 후 확정)
  constraints.txt                   런타임 고정 버전(C-02)
  constraints-dev.txt               개발 고정 버전(C-02)
  pytest.ini                        C-27
  bin/docctl                        C-26
  skills/docdb/SKILL.md             C-26
  catalogue/starter.json            C-23
  catalogue/item_types.json         C-23
  tools/matrix.sh                   C-27
  src/docdb_launch.py               C-04, C-24 (부트스트랩)
  src/docdb/__init__.py             __version__ = "0.1.0"
  src/docdb/__main__.py             sys.exit(cli.main())
  src/docdb/errors.py               C-05
  src/docdb/clock.py                C-07
  src/docdb/textnorm.py             C-06
  src/docdb/paths.py                C-04
  src/docdb/db.py                   C-08
  src/docdb/migrate.py              C-09
  src/docdb/schema/0001_base.sql … 0008_call_log_session.sql   C-09
  src/docdb/backup.py               C-09
  src/docdb/ids.py                  C-10
  src/docdb/actors.py               C-11
  src/docdb/history.py              C-15
  src/docdb/policy.py               C-16
  src/docdb/catalogue.py            C-23
  src/docdb/llm.py                  C-12~C-14
  src/docdb/relations.py            C-17
  src/docdb/workflow.py             C-18
  src/docdb/profile.py              C-19
  src/docdb/runs.py                 C-20
  src/docdb/importer.py             C-21
  src/docdb/summaries.py            C-22
  src/docdb/mcp_server.py           C-25
  src/docdb/cli.py                  C-24
  tests/conftest.py                 C-27
  tests/fixtures/…                  C-27
  tests/unit/ tests/legacy/ tests/integration/
```

### import 허용 규칙

| 모듈 | import 가능 | import 금지 |
|---|---|---|
| `errors`, `clock`, `textnorm`, `paths` (부트스트랩 안전 모듈) | 표준 라이브러리, 서로 간 | `docdb.db` 및 그 밖의 `docdb` 모듈, 서드파티 |
| `docdb_launch.py` | 표준 라이브러리, 부트스트랩 안전 모듈. venv 경로를 붙인 **뒤에만** `docdb.cli`, `docdb.mcp_server` | 그 밖의 모든 것 |
| `db` | 표준 라이브러리, `pysqlite3`, 부트스트랩 안전 모듈 | `mcp` |
| `mcp_server` | 표준 라이브러리, `mcp`, `docdb.*` | — |
| 그 밖의 `docdb.*` | 표준 라이브러리, `docdb.*` | `pysqlite3` 직접 import, 표준 `sqlite3`, `mcp` |

- `pysqlite3`는 `db.py`에서만 `from pysqlite3 import dbapi2` 형태로 import한다.
- 표준 라이브러리 `sqlite3` 모듈은 어디서도 import하지 않는다(테스트 포함).
- DB 예외는 `docdb.db.DatabaseError`, `docdb.db.IntegrityError`로만 잡는다.

---

## C-02 Python과 의존성 (D3, D4)

| 항목 | 값 |
|---|---|
| Python | 3.10 이상. 3.11 이상에만 있는 기능 금지: `tomllib`, `ExceptionGroup`/`except*`, `typing.Self`, `datetime.UTC`, `enum.StrEnum`, `asyncio.TaskGroup`, `typing.LiteralString` |
| 런타임 의존성 | `pysqlite3==0.6.1`, `mcp==2.3.0` (2026-10-13 설치 확인한 버전). 이 둘의 하위 의존성은 `constraints.txt`에 `pip freeze` 결과로 고정 |
| 개발 의존성 | `pytest` (T01에서 설치한 버전을 `constraints-dev.txt`에 고정). 그 밖에 추가 금지 |
| 네트워크 | 코드에서 네트워크 호출 금지. 예외: `setup`의 `pip install` |

`constraints-dev.txt`는 `-c constraints.txt` 한 줄과 `pytest==<버전>`, 그 하위 의존성 고정으로 구성한다.

---

## C-03 환경 변수

이 표에 없는 환경 변수를 읽지 않는다.

| 변수 | 읽는 곳 | 의미 |
|---|---|---|
| `DOCDB_HOME` | `paths.data_dir` | 데이터 폴더 덮어쓰기 |
| `DOCDB_PATH` | `paths.find_db` | DB 파일 경로 덮어쓰기(탐색 생략) |
| `DOCDB_START_DIR` | `paths.find_db` | DB 탐색 시작 폴더 |
| `DOCDB_ACTOR` | `actors.resolve_actor` | 기본 행위자 ID |
| `DOCDB_WORK` | `cli`, `mcp_server` | 현재 작업 ID(이력의 `work_id`, 규칙 R5) |
| `DOCDB_SESSION` | `cli`, `mcp_server` | 세션 표시(C-15 `session_ref`) |
| `DOCDB_MODEL` | `cli`, `mcp_server` | 모델 표시(C-15 `session_ref`) |
| `XDG_DATA_HOME`, `LOCALAPPDATA` | `paths.data_dir` | OS 표준 데이터 폴더 |

---

## C-04 경로 (D5, D13)

`paths.py` 공개 함수:

```python
DOT_DIR = ".docdb"
DB_FILE = "docdb.sqlite"

def data_dir() -> Path: ...
def venv_dir() -> Path: ...          # data_dir() / "venv"
def venv_python() -> Path: ...       # Windows: venv/"Scripts"/"python.exe", 그 외: venv/"bin"/"python"
def stamp_path() -> Path: ...        # venv_dir() / "docdb-stamp.json"
def backups_dir(project_id: str) -> Path: ...   # data_dir() / "backups" / project_id
def find_db(start: Path | None = None) -> Path: ...
def project_root(db_path: Path) -> Path: ...    # db_path.parent.parent (.docdb의 부모)
```

`data_dir()` 알고리즘(순서대로 첫 번째 해당):

1. `DOCDB_HOME`이 있으면 `Path(DOCDB_HOME)`.
2. `sys.platform == "win32"`: `Path(LOCALAPPDATA) / "docdb"`. `LOCALAPPDATA`가 없으면 `Path.home() / "AppData" / "Local" / "docdb"`.
3. `sys.platform == "darwin"`: `Path.home() / "Library" / "Application Support" / "docdb"`.
4. 그 외: `Path(XDG_DATA_HOME) / "docdb"`, 없으면 `Path.home() / ".local" / "share" / "docdb"`.

`find_db(start)` 알고리즘:

1. `DOCDB_PATH`가 있으면 그 경로. 파일이 없으면 `DocdbError("NO_DB", ...)`.
2. 시작 폴더 = `start` → `DOCDB_START_DIR` → `Path.cwd()` 중 첫 값. `resolve()`한다.
3. 시작 폴더와 그 모든 상위 폴더에서 차례로 `<폴더>/.docdb/docdb.sqlite`가 파일이면 반환.
4. 없으면 `DocdbError("NO_DB", "docctl init 실행 필요")`.

스탬프 파일 `docdb-stamp.json`:

```json
{"python": "3.12.3", "docdb": "0.1.0", "constraints_sha256": "<constraints.txt 파일 바이트의 sha256 hex>"}
```

`python` 값은 `platform.python_version()`이다.

---

## C-05 오류 모델

```python
class DocdbError(Exception):
    def __init__(self, code: str, detail: str = "") -> None: ...
    code: str
    detail: str
    exit_code: int          # 아래 표에서 code로 결정

ERROR_CODES: dict[str, int]  # code -> exit_code
def format_error(err: DocdbError) -> str: ...   # "ERROR <CODE> <detail>"
```

오류 코드 전체 목록(이 밖의 코드 금지):

| 코드 | 종료 코드 | 언제 |
|---|---:|---|
| `PY_TOO_OLD` | 2 | Python 3.10 미만 |
| `NO_VENV` | 2 | 데이터 폴더 venv 또는 스탬프 없음 |
| `VENV_MISMATCH` | 2 | 스탬프의 Python 버전(major.minor) ≠ 현재 인터프리터 |
| `DEP_MISSING` | 2 | venv에 `pysqlite3` 또는 `mcp` import 실패 |
| `SQLITE_CAPABILITY` | 2 | SQLite 3.34 미만, JSON 함수 없음, FTS5 trigram 없음 |
| `NO_DB` | 3 | DB 파일 없음 |
| `DB_EXISTS` | 3 | `init` 대상에 DB가 이미 있음 |
| `MIGRATION_REQUIRED` | 3 | DB 구조 버전 < 플러그인 최신 버전 |
| `DB_NEWER` | 3 | DB 구조 버전 > 플러그인 최신 버전 |
| `BAD_ARGS` | 1 | 인자 오류, 필수 값 없음 |
| `NOT_FOUND` | 1 | 대상 없음 |
| `NEEDS_HUMAN` | 1 | 사람·대화형 터미널(TTY) 필요 |
| `ID_FORMAT` | 1 | ID 형식 위반, 재사용 금지 위반 |
| `SECRET_DETECTED` | 1 | 민감 정보 패턴 발견(C-16) |
| `CONFLICT` | 1 | `expect` digest 불일치 |
| `ANCHOR_NOT_FOUND` | 1 | `replace_section` 제목 없음 |
| `UNSUPPORTED_HANDLE` | 1 | 그 연산에 쓸 수 없는 핸들 |
| `BAD_HANDLE` | 1 | 핸들 문법 위반(C-12) |
| `BAD_OP` | 1 | 알 수 없는 연산, 필수 키 누락, 함께 쓸 수 없는 키 |
| `DB` | 1 | DB 제약 위반(메시지 앞 80자 포함) |
| `DUPLICATE` | 1 | 같은 관계·ID가 이미 있음 |
| `REJECTED_BEFORE` | 1 | 반려된 관계 재제안 |
| `STATUS_FORBIDDEN` | 1 | 허용되지 않는 상태 전이(C-18) |
| `SUMMARY_TOO_LONG` | 1 | `sum`이 60 tok 초과(C-22) |
| `CATALOGUE_INCOMPATIBLE` | 1 | 값이 있는 종류·필드를 카탈로그에서 제거(C-23) |
| `IMPORT_STALE` | 1 | import 원본이 바뀌었거나 없음(C-21) |

- `ERROR_CODES`에 없는 `code`로 `DocdbError`를 만들면 `ValueError`를 던진다(오타 방지).
- `detail`의 사람용 안내는 한국어 한 줄로 쓴다. 코드는 영어 대문자다.
- 비밀 값, 파일 내용, 필드 본문을 `detail`에 넣지 않는다.
- `put`·`import label`·`link`처럼 여러 건을 처리하는 명령은 건별 오류를 예외가 아니라 `ERR <대상> <CODE> <detail>` 줄로 반환한다(C-14).

---

## C-06 텍스트 규칙 (D19)

`textnorm.py` 공개 함수:

```python
def normalize_text(s: str | None) -> str | None: ...
def canonical_json(obj: object) -> str: ...
def digest16(text: str | None) -> str: ...
def sha256_hex(data: bytes) -> str: ...
def tok_est(text: str | None) -> int: ...
def q(value: object) -> str: ...
def norm_key(s: str) -> str: ...
```

| 함수 | 규칙 |
|---|---|
| `normalize_text` | `None`이면 `None`. `"\r\n"`과 `"\r"`을 `"\n"`으로 바꾼 뒤 `unicodedata.normalize("NFC", …)`. 앞뒤 공백·빈 줄은 바꾸지 않는다 |
| `canonical_json` | 모든 문자열 값과 키를 `normalize_text`한 뒤 `json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))` |
| `digest16` | `sha256(normalize_text(text or "").encode("utf-8")).hexdigest()[:16]` |
| `sha256_hex` | `hashlib.sha256(data).hexdigest()` |
| `tok_est` | 원본과 같음: ASCII 문자 수 / 3.5 + 그 외 문자 수 / 1.0, 올림. `None`·빈 문자열은 0. 입력은 `normalize_text` 후 계산 |
| `q` | 원본과 같음: `str(value)`의 줄바꿈을 `" ⏎ "`로 바꾸고, 빈 문자열이거나 공백이 있으면 큰따옴표로 감싼다 |
| `norm_key` | 비교용 키: `normalize_text` → `casefold()` → 공백과 `_-·.:()[]/` 문자 제거 |

**모든 자유 텍스트**(필드 본문, 항목 제목, 이유, 요약, 인계 내용, import 본문)는 저장 전에 `normalize_text`를 거친다. JSON 값은 `canonical_json`으로 저장한다. digest는 항상 정규화된 값으로 계산한다.

---

## C-07 시간

`clock.py`:

```python
def now_iso() -> str: ...   # datetime.now(timezone.utc).isoformat(timespec="seconds") → "2026-10-13T00:00:00+00:00"
def utc_year() -> int: ...
```

모든 시각은 `clock.now_iso()`로만 얻는다(`datetime.now` 직접 호출 금지). 테스트는 이 두 함수를 monkeypatch한다.

---

## C-08 DB 연결과 트랜잭션 (D20)

`db.py` 공개 API:

```python
DatabaseError: type    # pysqlite3.dbapi2.DatabaseError
IntegrityError: type   # pysqlite3.dbapi2.IntegrityError
Connection = Any       # pysqlite3.dbapi2.Connection 별칭(타입 표기용)

def connect(path: Path, *, create: bool = False) -> Connection: ...
def check_capabilities(con: Connection) -> None: ...
def write_tx(con: Connection) -> ContextManager[None]: ...
def open_project_db(start: Path | None = None, db_path: Path | None = None) -> Connection: ...
def sqlite_version(con: Connection) -> tuple[int, int, int]: ...
```

`connect` 규칙(이 순서로):

1. `create=False`이고 파일이 없으면 `DocdbError("NO_DB")`.
2. `dbapi2.connect(str(path), isolation_level=None, timeout=5.0)` — 자동 커밋 모드. 트랜잭션은 `write_tx`로만 연다.
3. `PRAGMA foreign_keys=ON`
4. `PRAGMA busy_timeout=5000`
5. `PRAGMA journal_mode=WAL` — 결과가 `wal`이 아니면 stderr에 `WARN journal_mode=<결과>` 한 줄을 쓰고 계속한다.

`check_capabilities`: SQLite 버전 ≥ 3.34.0, `SELECT json_valid('{}')`가 1, `CREATE VIRTUAL TABLE temp._cap USING fts5(x, tokenize='trigram')` 성공 후 `DROP`. 하나라도 실패하면 `DocdbError("SQLITE_CAPABILITY", "<실패 항목>")`.

`write_tx` 규칙:

- 트랜잭션 밖에서 호출하면 `BEGIN IMMEDIATE` → 정상 종료 시 `COMMIT`, 예외 시 `ROLLBACK` 후 예외를 다시 던진다.
- 이미 트랜잭션 안이면 `SAVEPOINT sp_<깊이>` → 정상 종료 시 `RELEASE`, 예외 시 `ROLLBACK TO` 후 `RELEASE`, 예외를 다시 던진다.
- **모든 쓰기는 `write_tx` 안에서만** 한다. 읽기는 트랜잭션 없이 한다.
- ID 순번 발급(C-10)과 그 ID의 INSERT는 같은 `write_tx` 안에서 한다.

`open_project_db`: `db_path or paths.find_db(start)` → `connect` → `check_capabilities` → `migrate.ensure_current`. CLI·MCP는 이 함수로만 DB를 연다(`init`, `migrate`, `restore` 제외).

---

## C-09 마이그레이션과 백업 (D21, D22)

### 마이그레이션 파일

- 위치: `src/docdb/schema/`. 이름: 정규식 `^(\d{4})_[a-z0-9_]+\.sql$`. 번호는 1부터 빈틈없이 이어진다.
- 내용은 [../11-schema.md](../11-schema.md) 7.0절 표를 따른다.

| 번호 | 파일 | 내용 원천 |
|---|---|---|
| 1 | `0001_base.sql` | `docs-db/reference/docdb-package/docdb_schema.sql` **바이트 그대로 복사** |
| 2 | `0002_token_ext.sql` | `docdb_schema_token_ext.sql` **바이트 그대로 복사** |
| 3 | `0003_relation_status.sql` | 11-schema 7.1·7.2 + 7.7 시제품 SQL. 추가로 `DROP VIEW v_rtm;` 후 원본 정의에 `rel.status = 'confirmed'` 조건을 더해 다시 생성 |
| 4 | `0004_item_fts_ad.sql` | 11-schema 7.3·7.7 |
| 5 | `0005_import_segment.sql` | 11-schema 7.4 |
| 6 | `0006_catalogue_ext.sql` | 11-schema 7.5 `cat_kind_field.extract_rel`, 7.9 `cat_item_type.defined_by_field` |
| 7 | `0007_id_formats.sql` | 11-schema 7.6 트리거 |
| 8 | `0008_call_log_session.sql` | 11-schema 7.8 |

- 커밋된 마이그레이션 파일은 절대 고치지 않는다. 고칠 일이 생기면 새 번호를 추가한다.
- 마이그레이션 SQL 안에 `BEGIN`·`COMMIT`·`PRAGMA user_version`을 쓰지 않는다(실행기가 붙인다).

### `migrate.py`

```python
def schema_files() -> list[tuple[int, Path]]: ...      # 번호순
def latest_version() -> int: ...
def current_version(con: Connection) -> int: ...       # PRAGMA user_version
def ensure_current(con: Connection) -> None: ...       # MIGRATION_REQUIRED / DB_NEWER
def apply_pending(con: Connection) -> list[int]: ...   # 적용한 번호 목록
```

`apply_pending` 알고리즘: 번호마다 `con.executescript("BEGIN IMMEDIATE;\n" + sql + f"\nPRAGMA user_version = {n};\nCOMMIT;")`. 예외가 나면 `con.in_transaction`일 때 `ROLLBACK`을 실행하고 `DocdbError("DB", f"migration {n:04d}: <메시지 앞 80자>")`를 던진다. `executescript`는 시작 전에 열린 트랜잭션을 커밋하므로 `write_tx` 안에서 부르지 않는다.

### `backup.py`

```python
def backup(con: Connection, project_id: str, reason: str) -> Path: ...
def restore(db_path: Path, backup_file: Path) -> None: ...
```

- 파일 이름: `<YYYYMMDDTHHMMSSZ>-<reason>.sqlite`(UTC, `reason`은 `[a-z0-9-]+`만), 위치 `paths.backups_dir(project_id)`. 같은 이름이 있으면 `-2`, `-3`을 붙인다.
- `con.backup(dest_con)` API로 복사한다(파일 복사 금지 — WAL 상태에서 깨질 수 있음).
- 자동 백업 시점: `migrate` 적용 전(`pre-migrate`), `import scan` 전(`pre-import-scan`), `import label` 전(`pre-import-label`), `restore` 전(`pre-restore`).
- 보존 기간 정리는 하지 않는다(O18 미결).

---

## C-10 ID (D17)

`ids.py`:

```python
PATTERNS: dict[str, str]
PREFIXES: dict[str, str]   # {"work": "WORK", "run": "RUN", "handoff": "HO", "approval": "APR", "artifact": "ART", "exception": "PEX"}
def validate(kind: str, value: str) -> None: ...      # 위반 시 DocdbError("ID_FORMAT")
def next_id(con: Connection, kind: str, year: int | None = None) -> str: ...
def ensure_not_reused(con: Connection, entity_type: str, entity_id: str) -> None: ...
```

| kind | 정규식 | 발급 |
|---|---|---|
| `doc` | `^DOC-[A-Z0-9]+(-[A-Z0-9]+)*$`, 길이 ≤ 64 | 지정(의미 있는 이름) |
| `item` | `^[A-Z0-9]+-[A-Z0-9]+(-[A-Z0-9]+)*$`, 길이 ≤ 64, `cat_item_type.id_prefix`로 시작 | 지정 |
| `work` | `^WORK-\d{4}-\d{4,}$` | 자동 |
| `run` | `^RUN-\d{4}-\d{4,}$` | 자동 |
| `handoff` | `^HO-\d{4}-\d{4,}$` | 자동 |
| `approval` | `^APR-\d{4}-\d{4,}$` | 자동 |
| `artifact` | `^ART-\d{4}-\d{4,}$` | 자동 |
| `exception` | `^PEX-\d{4}-\d{4,}$` | 자동 |

`next_id`: `year = year or clock.utc_year()`. 해당 테이블에서 `id LIKE '<PREFIX>-<year>-%'`인 ID들의 마지막 `-` 뒤 정수 최댓값 + 1(없으면 1)을 `f"{PREFIX}-{year}-{n:04d}"`로 만든다. 반드시 `write_tx` 안에서 부르고 같은 트랜잭션에서 INSERT한다. 트랜잭션 밖(`con.in_transaction`이 거짓)에서 부르면 `DocdbError("DB", "next_id는 write_tx 안에서만")`.

`ensure_not_reused`: `change_log`에 `entity_type`·`entity_id`가 같고 `op='delete'`인 행이 있으면 `DocdbError("ID_FORMAT", "삭제된 ID 재사용 금지")`. 문서·항목 생성 때 호출한다.

---

## C-11 행위자 (D18, D24)

`actors.py`:

```python
class Actor(NamedTuple):
    id: str
    type: str          # "human" | "ai" | "team"

ACTOR_PATTERNS: dict[str, str]
def add_actor(con: Connection, actor_id: str, actor_type: str, name: str, by: Actor | None) -> None: ...
def resolve_actor(con: Connection, explicit: str | None) -> Actor: ...
def require_human(actor: Actor, interactive: bool) -> None: ...
def require_ai(actor: Actor) -> None: ...
```

| 종류 | 정규식 | 예 |
|---|---|---|
| `human` | `^u-[a-z0-9][a-z0-9._-]{0,62}$` | `u-jjm` |
| `ai` | `^ai-[a-z0-9][a-z0-9-]{0,62}$` | `ai-claude`, `ai-codex` |
| `team` | `^team-[a-z0-9][a-z0-9-]{0,62}$` | `team-ci` |

- `add_actor`: ID가 `actor_type`의 정규식에 맞지 않으면 `ID_FORMAT`. 이력(`actor` insert)을 남긴다.
- `resolve_actor`: `explicit` → `DOCDB_ACTOR` 순. 둘 다 없으면 `BAD_ARGS`. 없거나 `active=0`이면 `NOT_FOUND`.
- `require_human`: 행위자가 `human`이 아니거나 `interactive`가 거짓이면 `NEEDS_HUMAN`. CLI는 `sys.stdin.isatty() and sys.stdout.isatty()`로 `interactive`를 정해 넘긴다. 에이전트가 셸로 실행하는 명령에는 TTY가 없으므로 사람 전용 명령을 실행할 수 없다. **이것은 실수 방지 장치이며 보안 경계가 아니다.**
- `require_ai`: 행위자가 `ai`가 아니면 `DocdbError("BAD_ARGS", "MCP 행위자는 ai여야 함")`.
- `init`이 기본으로 등록하는 행위자: `--owner`로 받은 사람, `ai-claude`("Claude Code"), `ai-codex`("Codex"), `team-ci`("CI").

---

## C-12 핸들 (원본 토큰 명세 4.1 + 확장)

| 핸들 | 정규식 | 대상 테이블 | 관계 끝점 타입 |
|---|---|---|---|
| `d:` | `^d:(DOC-[A-Z0-9]+(?:-[A-Z0-9]+)*)$` | `document` | `document` |
| `f:` | `^f:(DOC-[A-Z0-9]+(?:-[A-Z0-9]+)*)/([A-Z][0-9]{2})/([a-z][a-z0-9_]*)$` | `field_value` | — |
| `i:` | `^i:([A-Z0-9]+-[A-Z0-9]+(?:-[A-Z0-9]+)*)$` | `item` | `item` |
| `w:` | 아래 코드 블록 | `work` | `work` |
| `r:` | `^r:(RUN-\d{4}-\d{4,})$` | `run` | `run` |
| `a:` | `^a:(ART-\d{4}-\d{4,})$` | `artifact` | `artifact` |
| `k:` | `^k:([A-Z][0-9]{2})$` | `cat_kind` | — |
| `s:` | `^s:(\d+)$` | `import_segment` | — |
| `d?:` | `^d\?:(ART-\d{4}-\d{4,})$` | 종류 미정 import 파일 | — |

`w:` 정규식(`new`는 작업 생성용):

```text
^w:(WORK-\d{4}-\d{4,}|new)$
```

핸들 파싱 함수는 `llm.py`의 `parse_handle(h: str) -> tuple[str, tuple[str, ...]]` 하나로만 구현하고 다른 모듈은 이것을 import한다. 문법 위반은 `BAD_HANDLE`.

---

## C-13 응답 형식

- **원본 `docctl_llm.py`의 출력 형식을 그대로 유지한다.** 머리줄(`#<tool> …`), 줄머리 기호(`W`, `>`, `!`, `D`, `I`, `f:`, `i:`, `d:`), `UNCHANGED`, `OK …`, `ERR …`, `APPLIED/NOTHING_APPLIED ok=<n> err=<n>`, `omitted=<n>`, `… omitted → get <handle> / since <cursor>` 줄이 기준이다. 원본 테스트의 기대값이 형식 명세다.
- 예산 처리(`Out` 클래스: 강제 줄은 예산과 무관하게 포함, 넘친 줄은 `omitted` 증가)도 원본 그대로다.
- 새로 추가하는 줄(이 밖의 새 줄 형식 금지):

| 줄 | 쓰는 곳 | 형식 |
|---|---|---|
| 확정 대기 관계 | `ctx` 차단 요인, 강제 줄 | `! pending_links=<n>` (n>0일 때만) |
| 필수 연결 누락 | `ctx` 차단 요인(P2, 예산 안) | `! missing_link d:<doc_id> k:<kind_id>` |
| 정책 차단 | `get`·`ctx`·`find`·`since` | 해당 줄 끝에 ` policy=local_only`, 본문·요약·발췌·diff 생략 |
| 규칙 알림 | `put` 응답 | `NOTE R1 skip <item_id> defined_in=<doc_id>` |
| 관계 목록 | `link review` | `rel=<id> <from_handle> -<rel>-> <to_handle> by=<actor> why=<q(why)>` |

---

## C-14 `DocDB`와 `put` 패치 (D10, D15, D16)

```python
class DocDB:
    def __init__(self, con: Connection, actor: Actor, work: str | None = None,
                 session: str | None = None, model: str | None = None) -> None: ...
    def ctx(self, work_id: str, budget: int = 1500) -> str: ...
    def get(self, handles: list[str], level: int = 1, known: dict[str, str] | None = None, budget: int = 2000) -> str: ...
    def find(self, query: str, limit: int = 10, budget: int = 800) -> str: ...
    def trace(self, handle: str, rel_types: list[str] | None = None, depth: int = 1, budget: int = 800) -> str: ...
    def since(self, cursor: int, work: str | None = None, budget: int = 1200, diff_lines: int = 6) -> str: ...
    def put(self, patches: list[dict], reason: str | None = None) -> str: ...
```

메서드 이름·인자·기본값은 원본 `docctl_llm.DocDB`와 같다(생성자만 다름). `get`에 문법이 틀린 핸들이 오면 그 핸들 자리에 `ERR <h> BAD_HANDLE` 줄을 쓰고 나머지는 계속 처리한다.

`DocDB.put(patches: list[dict], reason: str | None = None) -> str`. 한 번의 `put`은 하나의 `write_tx`다. **하나라도 `ERR`이면 전부 취소**하고 `NOTHING_APPLIED`를 반환한다(원본과 같음). 응답에 본문을 되돌려주지 않는다.

### 필드 쓰기 (원본 + `sum`)

```json
{"h": "f:DOC-X/D01/behavior", "op": "set|append|replace_section", "text": "...",
 "expect": "<dg>", "anchor": "## 제목", "state": "filled|tbd|unknown|not_applicable", "why": "...", "sum": "..."}
```

처리 순서(이 순서를 바꾸지 않는다): 핸들 파싱 → 문서·종류 존재 확인(`NOT_FOUND`) → `expect` 비교(`CONFLICT now=<dg>`) → 새 값 계산(`set`/`append`/`replace_section`, 원본 함수 그대로) → `normalize_text` → 민감 정보 검사(C-16) → `field_value` UPSERT(digest·tok_est 포함) → 이력(C-15) → `sum` 처리(C-22) → 규칙 R1·R2·R3·R5(C-17).

응답: `OK f:… dg=<16> tok=<n>`.

### 항목 쓰기

```json
{"h": "i:REQ-HW-002", "op": "upsert", "title": "...", "status": "...",
 "attrs": {...} | "attrs_patch": {...}, "defined_in": "d:DOC-X", "expect": "<dg>"}
```

- 생성 시 `title` 필수. `attrs`(전체 교체)와 `attrs_patch`(얕은 병합, 값이 `null`이면 키 삭제)는 함께 쓸 수 없다(`BAD_OP`).
- `item_type`은 `cat_item_type.id_prefix` 중 ID가 시작하는 가장 긴 접두어로 정한다. 없으면 `ID_FORMAT`.
- 생성 시 `ids.validate("item")`, `ids.ensure_not_reused`.
- 항목 digest = `digest16(canonical_json({"title", "status", "attrs", "defined_in"}))`, `item.digest`·`item.tok_est`에 저장.
- 응답: `OK i:<ID> dg=<16> tok=<n>`.

### 관계 제안·철회

```json
{"op": "link", "from": "i:REQ-HW-002", "rel": "verified_by", "to": "i:TC-HW-002", "why": "..."}
{"op": "unlink", "rel_id": 41, "why": "..."}
```

- `link`: 끝점 핸들은 `d:`·`i:`·`w:`·`r:`·`a:`만(그 밖은 `UNSUPPORTED_HANDLE`). 끝점이 없으면 `NOT_FOUND`. `rel`은 12종 중 하나. `why`는 비어 있으면 `BAD_OP`. 같은 (출발, 종류, 도착)이 있으면 상태가 `rejected`일 때 `ERR … REJECTED_BEFORE rel=<id>`, 그 밖은 `ERR … DUPLICATE rel=<id> status=<s>`. 관계 종류와 끝점 타입이 C-17 "허용 끝점" 표에 맞지 않으면 `BAD_OP`. 성공 시 `relation(status='proposed', proposed_by=<actor>, reason=<why>)`를 넣고 `source`는 행위자가 `ai`면 `'agent'`, `human`이면 `'human'`이다. **행위자와 관계없이 상태는 항상 `proposed`**이며 확정은 `link confirm`(사람·TTY)으로만 한다. `team` 행위자의 `link`는 `BAD_ARGS`. 응답 `OK rel=<id> proposed`.
- `unlink`: `status='proposed'`이고 `proposed_by`가 현재 행위자일 때만 삭제 → `OK unlink rel=<id>`. 그 밖은 `NEEDS_HUMAN`.

### 작업·인계 (D15, D24, D25)

```json
{"h": "w:new", "op": "work", "title": "...", "work_type": "feature", "owner": "u-jjm", "base_refs": ["pmt:docdb/_default/Work1-1"]}
{"h": "w:WORK-2026-0001", "op": "work", "status": "active"}
{"h": "w:WORK-2026-0001", "op": "handoff", "from_role": "...", "to_role": "...",
 "payload": {"done": [], "undone": [], "risks": [], "next_actions": [], "not_run": [], "as_of": 57}}
```

- 작업 생성: `owner`는 필수이며 `human` 행위자여야 한다(S-07, 아니면 `BAD_ARGS`). `base_refs` 형식 위반도 `BAD_ARGS`. `base_refs` 원소는 `^(pmt|git|doc):.+$`. 초기 상태 `planned`. 응답 `OK w:<WORK-ID> created`.
- 상태 변경: C-18 전이표와 권한을 따른다. 응답 `OK w:<WORK-ID> status=<s>`.
- 인계: `payload`의 다섯 목록 키는 필수(빈 배열 허용), `as_of`는 정수. 응답 `OK handoff=<HO-ID>`.

### 공통

- `reason` 인자는 이력의 `reason`에 저장한다. 패치별 `why`가 있으면 그것을 우선한다.
- 규칙 R5에 쓰는 작업 ID는 `DocDB(work=…)` 생성 인자(CLI `--work` 또는 `DOCDB_WORK`)다.

---

## C-15 이력 (D19, 원 설계 DEC-06)

`history.py`:

```python
RECORDED_TABLES: frozenset[str]
def append_change(con: Connection, entity_type: str, entity_id: str, op: str,
                  before: dict | None, after: dict | None, actor_id: str,
                  work_id: str | None, reason: str | None) -> int: ...
def row_digest(entity_type: str, entity_id: str, seq: int, op: str, after: dict | None, prev_digest: str | None) -> str: ...
def verify_chain(con: Connection, entity_type: str | None = None, entity_id: str | None = None) -> list[str]: ...
```

- 이력을 남기는 테이블(`RECORDED_TABLES`): `actor`, `project`, `fact_value`, `document`, `document_kind`, `field_value`, `item`, `relation`, `artifact`, `work`, `work_event`, `run`, `finding`, `handoff`, `approval`, `policy_exception`, `link_exemption`.
- 이력을 남기지 않는 테이블: `cat_*`(카탈로그는 `cat_version`과 `entity_type='catalogue'` 이력 1건), `kind_applicability`, `call_log`, `context_read`, `text_unit`, `fts_text`, `import_segment`, `entity_summary`, `generation`, `doc_revision`(자체 체인).
- `entity_id` 형식: 단일 키는 그 값, 복합 키는 `|`로 연결(`field_value`: `doc|kind|field`, `fact_value`: `project|fact_key`, `document_kind`: `doc|kind`, `link_exemption`: `doc|target_kind`, `work_event`: `work|fact_key`), `relation`·`finding`은 정수 ID의 문자열.
- `append_change`를 트랜잭션 밖에서 부르면 `DocdbError("DB", "append_change는 write_tx 안에서만")`.
- `before_json`·`after_json`은 `canonical_json`으로 저장한다.
- `before`·`after`: 쓰기 대상 컬럼만 담은 dict. 생성은 `before=None`, 삭제는 `after=None`. 필드 본문은 전문을 저장한다(O14 미결, 기본값 = 원본 동작).
- `row_digest = "sha256:" + sha256_hex(canonical_json({"t": entity_type, "e": entity_id, "seq": seq, "op": op, "after": after, "prev": prev_digest}).encode("utf-8"))`.
- `append_change`는 직전 행을 `ORDER BY seq DESC LIMIT 1`로 찾아 `seq`, `prev_change_id`, `prev_digest`를 채운다. 반드시 같은 `write_tx` 안에서 호출한다.
- `verify_chain`은 각 행의 `row_digest`를 다시 계산해 다르면 `"<entity_type>|<entity_id> seq=<n> digest mismatch"` 문자열을 목록에 넣는다(문제 없으면 빈 목록).
- `call_log.session_ref` = `DOCDB_SESSION`과 `DOCDB_MODEL`을 `"<session>/<model>"`로 연결(없는 쪽은 빈 문자열, 둘 다 없으면 NULL).

---

## C-16 정책 (D8, D23)

`policy.py`:

```python
SECRET_PATTERNS: list[tuple[str, str]]   # (이름, 정규식) — 아래 표 그대로, 순서 유지
def scan_secrets(text: str | None) -> list[str]: ...           # 걸린 패턴 이름 목록
def assert_no_secrets(con: Connection, handle: str, texts: list[str | None]) -> None: ...
def allow_secret(con: Connection, handle: str, reason: str, days: int, actor: Actor, interactive: bool) -> str: ...
def can_export(con: Connection, doc_id: str) -> bool: ...
```

`SECRET_PATTERNS`의 내용(이 순서, 이 문자열 그대로):

```python
SECRET_PATTERNS = [
    ("aws_access_key", r"AKIA[0-9A-Z]{16}"),
    ("github_token", r"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("anthropic_key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("openai_key", r"sk-(?!ant-)[A-Za-z0-9_-]{20,}"),
    ("slack_token", r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("telegram_bot_token", r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("assignment", r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token)\b\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
]
```

- `assert_no_secrets`: 걸린 패턴이 있고 유효한 예외가 없으면 `DocdbError("SECRET_DETECTED", "<패턴 이름 목록>")`. 비밀 값 자체는 어디에도 출력·기록하지 않는다.
- 유효한 예외: `policy_exception`에서 `scope = "secret:" + handle`이고 `expires_at > now_iso()`이며 `approved_by`가 `human`인 행.
- `allow_secret`은 사람 전용(`require_human`), `exception_id = next_id("exception")`, `kind_id=NULL`, 이력 기록.
- 검사 대상: `field_value` 본문(텍스트·JSON 직렬화), 항목 제목·`attrs`, 관계 `why`, `sum`, 인계 `payload`, 실행 적재 `findings[].message/evidence`, import 적용 본문.
- `can_export`: `document.export_policy != 'local_only'`. `classification`은 보지 않는다(O20 미결). 적용 위치는 C-13의 "정책 차단" 줄.

---

## C-17 관계와 규칙 추출 (D10, D11, D12)

`relations.py`:

```python
REL_TYPES: tuple[str, ...]   # 12종, 0001_base.sql CHECK와 같은 순서
def propose(con, actor: Actor, from_h: str, rel: str, to_h: str, why: str) -> str: ...      # put의 link가 호출, 응답 줄 반환
def withdraw(con, actor: Actor, rel_id: int, why: str) -> str: ...                          # put의 unlink
def review(con, work_id: str | None = None) -> list[str]: ...                              # C-13 관계 목록 줄
def confirm(con, actor: Actor, rel_ids: list[int], interactive: bool) -> list[str]: ...
def reject(con, actor: Actor, rel_id: int, why: str, interactive: bool) -> str: ...
def remove(con, actor: Actor, rel_id: int, why: str, interactive: bool) -> str: ...
def after_field_write(con, actor: Actor, doc_id: str, kind_id: str, field_key: str, text: str | None, work_id: str | None) -> list[str]: ...
def after_item_write(con, actor: Actor, item_id: str, work_id: str | None) -> list[str]: ...
def apply_run_coverage(con, actor: Actor, run_id: str, item_ids: list[str]) -> None: ...
def pending_count(con, work_id: str | None = None) -> int: ...
def missing_links(con, doc_ids: list[str] | None = None) -> list[tuple[str, str]]: ...
```

`after_*`는 `NOTE` 줄 목록을 반환한다(C-13). 규칙 관계는 `source='rule:Rn'`, `status='confirmed'`, `proposed_by=NULL`, `anchor`에 출처 핸들을 둔다. 규칙이 만든 관계의 추가·삭제도 `change_log`에 기록하며, 행위자는 규칙을 일으킨 행위자, `reason='rule:Rn'`이다.

### 허용 끝점 (D11)

`link` 제안과 규칙 추출이 만드는 관계는 아래 끝점 타입 조합만 허용한다(방향은 [../02-data-model.md](../02-data-model.md) 5절). 항목의 세부 종류(요구사항·테스트 케이스 등)는 검사하지 않는다.

| 관계 | 출발 타입 | 도착 타입 |
|---|---|---|
| `verified_by` | item | item, run |
| `implements` | item, artifact | item |
| `changes` | work | document, item |
| `supersedes` | document → document, item → item (같은 타입끼리) | — |
| `generated_from` | artifact, document | artifact, document, item |
| `evidence_for` | run, artifact | item, work, document |
| `depends_on` | item | item |
| `constrained_by` | item, document | item, document |
| `uses_data` | item | item |
| `visualizes` | item | item |
| `references` | document, item, artifact, work, run | document, item, artifact, work, run |
| `handoff_to` | work | work |

ID 추출 정규식(공통): 알려진 접두어 목록 P(`cat_item_type.id_prefix`)에 대해 `\b(P[A-Z0-9]+(?:-[A-Z0-9]+)*)\b`. 접두어가 여러 개 맞으면 가장 긴 접두어.

| 규칙 | 계기 | 동작 | anchor |
|---|---|---|---|
| R1 정의 | `f:` 쓰기, `(kind_id, field_key)`가 어떤 `cat_item_type`의 `(defined_by_kind, defined_by_field)`와 같음 | 그 필드의 각 줄에서 해당 접두어 ID 추출. 제목 = 줄에서 ID 뒤 텍스트의 앞쪽을 아래 `R1_TITLE_STRIP` 정규식으로 걷어 낸 값(최대 200자, 비면 ID). 항목이 없으면 생성(`defined_in`=문서), 같은 문서에 정의되어 있고 제목이 다르면 제목 갱신, 다른 문서에 정의되어 있으면 건드리지 않고 `NOTE R1 skip …`. 필드에서 사라진 ID의 항목은 삭제하지 않는다 | — |
| R2 언급 | 모든 `f:` 쓰기 | 문서의 모든 필드 본문에서 **이미 있는** 항목 ID 중 `defined_in`이 이 문서가 아닌 것의 집합 M을 구한다. `relation(document → item, references, rule:R2, anchor='d:<doc>')`을 M과 같게 맞춘다(없으면 추가, M에 없는 기존 R2 관계는 삭제) | `d:<doc>` |
| R3 필드 규칙 | `f:` 쓰기, 필드에 `extract_rel`이 있음 | 각 줄에서 `from_prefix` ID들과 `to_prefix` ID들(둘 다 이미 있는 항목)을 찾아 모든 쌍에 `relation(item → item, <rel>, rule:R3)`. 이 필드 anchor의 기존 R3 관계를 새 집합과 같게 맞춘다 | `f:<doc>/<kind>/<field>` |
| R4 실행 | `run ingest`의 `covers` | 각 항목에 `relation(run → item, evidence_for, rule:R4)` | `r:<run>` |
| R5 작업 변경 | 작업 ID가 있는 상태의 `f:`·`i:` 쓰기 | `relation(work → document 또는 item, changes, rule:R5)`. 이미 있으면 그대로 | `w:<work>` |

R1 제목 정리 정규식:

```python
R1_TITLE_STRIP = r"^[\s:|\-–—.)\]]+"
```

- 같은 (출발, 종류, 도착)이 어떤 상태로든 이미 있으면 규칙은 새로 넣지 않는다(사람의 반려를 규칙이 뒤집지 않는다).
- R2·R3의 "맞춘다"는 `source`가 같은 규칙이고 `anchor`가 같은 관계만 대상으로 한다. 에이전트·사람 관계는 규칙이 삭제하지 않는다.
- `confirm`·`reject`·`remove`는 사람 전용. `confirm`·`reject`는 `status='proposed'`만 대상, `decided_by`·`decided_at` 기록. `remove`는 어떤 상태든 삭제하되 `source LIKE 'rule:%'`는 거부(`BAD_OP`, "규칙 관계는 원본 본문을 고쳐서 바꿈").
- `missing_links`는 0003의 `v_missing_required_links`를 읽는다.
- `pending_count(con, work_id)`·`review(con, work_id)`의 작업 필터: 그 관계의 insert 이력(`change_log`, `entity_type='relation'`, `op='insert'`)의 `work_id`가 같은 것. `work_id=None`이면 전체. `ctx`는 `pending_count(con)`(전체)를 쓴다.

---

## C-18 작업·인계 (D15, D24, D25)

`workflow.py`:

```python
TRANSITIONS: dict[str, tuple[str, ...]]
AI_ALLOWED: frozenset[str]   # {"planned", "active", "blocked", "review"}
def create_work(con, actor: Actor, title: str, work_type: str, owner: str, base_refs: list[str]) -> str: ...
def set_status(con, actor: Actor, work_id: str, status: str, interactive: bool) -> str: ...
def create_handoff(con, actor: Actor, work_id: str, from_role: str, to_role: str, payload: dict) -> str: ...
def ack_handoff(con, actor: Actor, handoff_id: str, baseline: str, evidence: list[str], accept: bool, interactive: bool) -> str: ...
```

| 현재 | 갈 수 있는 상태 |
|---|---|
| `planned` | `active`, `cancelled` |
| `active` | `blocked`, `review`, `cancelled` |
| `blocked` | `active`, `cancelled` |
| `review` | `active`, `verified` |
| `verified` | `closed`, `active` |
| `closed`, `cancelled` | 없음 |

- 전이표에 없는 전이는 `STATUS_FORBIDDEN`. 목표 상태가 `AI_ALLOWED` 밖이면 `require_human`(`NEEDS_HUMAN`).
- `closed`가 되면 `closed_at = now_iso()`.
- `work_type`은 자유 문자열이되 `[a-z_]+`.
- `ack_handoff`: 사람 전용, `baseline` 필수(비면 `BAD_ARGS`), `ack_status`를 `accepted`/`rejected`로.
- docdb는 PMT를 호출하지 않는다. PMT 연결은 `base_refs`의 `pmt:` 문자열뿐이다(D25).
- `project.project_id`는 `init --project`로 받은 PMT slug다(`^[a-z0-9][a-z0-9-]{0,62}$`).

---

## C-19 프로필·적용 판정

`profile.py`:

```python
def evaluate(rule: dict, facts: dict[str, int | None], selected: set[str]) -> bool | None: ...
def unknown_facts(con, project_id: str) -> list[str]: ...
def set_fact(con, actor: Actor, project_id: str, fact_key: str, value: bool | None, reason: str, interactive: bool) -> None: ...
def recompute(con, project_id: str) -> dict[str, int]: ...   # {"true": n, "false": n, "unknown": n}
```

- 규칙 문법(원본 `test_scenarios.ev`와 같음, eval 금지): `{"always": true}` → True, `{"fact": k}` → 값 1/0/None을 True/False/None으로, `{"selected_kind": k}` → `k in selected`, `{"all": [...]}`, `{"any": [...]}`(3값 논리는 원본과 같음). 그 밖의 키는 `BAD_ARGS`.
- `set_fact`는 사람 전용, `cap.*`만, `project.profile_revision += 1`, 이력 기록.
- `recompute`는 모든 종류의 `applies_when`·`required_when`을 평가해 `kind_applicability`를 다시 쓴다(`reason` = `required_when`의 `canonical_json`).

---

## C-20 실행 결과 적재

`runs.py`:

```python
def ingest(con, actor: Actor, payload: dict) -> str: ...   # 반환: run_id
```

입력 JSON(키 이름 고정):

```json
{"run_type": "test", "work_id": "WORK-2026-0001", "target_ref": "<커밋 또는 digest>", "result": "PASS",
 "runner": "pytest", "trust_source": "ci", "env_item_id": "RES-DB01",
 "command_argv": ["pytest", "-q"], "started_at": "…", "ended_at": "…", "exit_code": 0,
 "target_versions": {}, "env_snapshot": {}, "limitations": null,
 "raw_log": {"uri": "logs/run.txt", "digest": "sha256:<hex>"},
 "covers": ["TC-HW-001"],
 "findings": [{"code": "DRIFT", "severity": "warning", "target_type": "item", "target_id": "RES-DB01",
               "message": "…", "evidence": "…"}]}
```

- 필수: `run_type`, `target_ref`, `result`, `runner`, `trust_source`. 나머지는 선택. 필수 키 누락이나 `result`가 5종(PASS, FAIL, NOT_RUN, ERROR, UNKNOWN) 밖이면 INSERT 전에 `BAD_ARGS`.
- 행위자가 `ai`면 `trust_source`를 `ai_reported`로 강제한다.
- `raw_log`가 있으면 `artifact(role='evidence', source_mode='external_reference')`를 만들고 `run.raw_artifact_id`에 연결.
- `covers`의 항목이 없으면 `NOT_FOUND`(전체 취소). R4 실행.
- 실행 기록은 고칠 수 없다(트리거). 다시 적재하면 새 `run_id`.

---

## C-21 import (D6)

`importer.py`:

```python
SUPPORTED_SUFFIXES = (".md", ".markdown", ".txt")
def scan(con, actor: Actor, folder: Path, owner: str, export_policy: str) -> str: ...
def next_batch(con, budget: int = 1500) -> str: ...
def label(con, actor: Actor, labels: list[dict]) -> str: ...
def report(con) -> str: ...
def split_segments(raw: bytes) -> list[dict]: ...   # {"seq","heading","start_off","end_off","text"}
```

| 단계 | 규칙 |
|---|---|
| 대상 폴더 | 프로젝트 루트(`.docdb`의 부모) 안이어야 함(아니면 `BAD_ARGS`). 하위 폴더를 이름순으로 순회, 이름이 `.`으로 시작하는 폴더와 `.docdb`는 건너뜀 |
| 파일 등록 | `artifact(uri=<프로젝트 루트 기준 POSIX 상대 경로>, digest="sha256:<원본 바이트 hex>", role='primary', source_mode='authored')`. 같은 `uri`와 `digest`가 이미 있으면 건너뜀 |
| 지원 판정 | 확장자가 `SUPPORTED_SUFFIXES`이고 UTF-8(앞 BOM 허용)로 디코드되면 지원. 아니면 등록만 하고 구간을 만들지 않음 |
| 분할 | 줄 단위. ```` ``` ```` 또는 `~~~`로 시작하는 줄이 코드 블록을 열고 닫는다. 코드 블록 밖에서 `^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$`에 맞는 줄이 구간 시작. 구간 = 제목 줄부터 다음 제목 줄 전까지(수준 무관 평면 분할). 첫 제목 앞 내용은 공백만이 아니면 `seq=0`, `heading=NULL` 구간 |
| 오프셋 | 원본 바이트 기준 `[start_off, end_off)`. `text_digest = digest16(구간 텍스트)` |
| 문서 종류 규칙 | 파일 이름(확장자 제외) 또는 첫 `#` 제목의 `norm_key`가 어떤 종류의 `title_ko` 또는 `is_primary=1` 별칭의 `norm_key`와 같으면 그 종류. 두 종류 이상 맞으면 미정 |
| 필드 규칙 | 종류가 정해진 파일에서, 구간 제목의 `norm_key`가 필드 키 또는 필드 설명의 첫 마디(처음 나오는 `(`·`,`·`—`·`:` 앞)의 `norm_key`와 같으면 `ruled`. 아니면 `pending` |
| 적용 | 본문 = 구간 텍스트에서 제목 줄을 뺀 나머지의 앞뒤 빈 줄 제거. 비면 `skipped(why='empty')`. 문서가 없으면 생성: `doc_id` = `DOC-<kind_id>-<파일 이름의 ASCII 영숫자 대문자, 최대 32자>`(비면 `DOC-<kind_id>-IMP-<artifact 순번 4자리>`), 충돌 시 `-2`, `-3`. 제목 = 첫 `#` 제목 또는 파일 이름. `lifecycle`·`category`는 종류 기본값, `authority='normative'`, `baseline_mode='desired'`, `owner_id=owner`, `export_policy=<scan 옵션>`. 같은 필드의 여러 구간은 `seq` 순으로 `append`(구분 `"\n\n"`). 쓰기는 `put`과 같은 내부 경로(C-14 필드 쓰기 처리 순서) |
| 재확인 | 적용 직전 원본 바이트를 다시 읽어 구간 digest를 비교. 다르거나 파일이 없으면 `status='stale'`, `ERR s:<id> IMPORT_STALE` |
| 백업 | `scan`·`label` 시작 시 `backup.backup` |

`next_batch` 출력:

```text
#import-next pending=<n> tok=<used>/<budget> omitted=<n>
d?:ART-2026-0003 "docs/old/서버 목록.md" heads=개요|서버|연락처
s:12 "임계값 설정" tok=88 | <앞 200자, 줄바꿈은 " ⏎ "> | cand=D01/behavior,D01/scope
```

종류 미정 파일(`d?:`)을 먼저, 그다음 `pending` 구간을 `seg_id` 순으로. 후보(`cand`)는 그 종류의 필드 중 `difflib.SequenceMatcher(None, norm_key(heading), norm_key(field_key 또는 설명 첫 마디)).ratio()` 상위 5개(동률은 필드 `sort_order` 순). `local_only` 파일의 구간은 발췌 없이 ` policy=local_only`.

`label` 입력 원소(이 셋 외 금지):

```json
{"doc": "d?:ART-2026-0003", "kind": "K02"}
{"seg": "s:12", "kind": "D01", "field": "behavior"}
{"seg": "s:13", "skip": true, "why": "목차"}
```

`doc` 라벨은 그 파일 구간들에 필드 규칙을 다시 적용한다. 응답 줄: `OK d?:<ART> kind=<K> ruled=<n>`, `OK s:<id> applied`, `OK s:<id> skipped`, `ERR s:<id> <CODE> <detail>`. 한 번의 `label`은 하나의 `write_tx`(하나라도 ERR이면 전부 취소).

`report` 출력:

```text
#import-report files=<n> supported=<n> unsupported=<n> segments=<n>
status pending=<n> ruled=<n> labeled=<n> skipped=<n> applied=<n> stale=<n>
```

---

## C-22 요약 (D16)

`summaries.py`:

```python
MAX_SUM_TOK = 60
def template(con, handle: str) -> str | None: ...    # 구조 값의 요약(저장하지 않음)
def store(con, actor: Actor, handle: str, text: str, source_digest: str) -> None: ...
def lookup(con, handle: str, current_digest: str) -> tuple[str, str | None]: ...   # ("fresh"|"stale"|"none", 요약)
```

| 대상 | 요약 |
|---|---|
| 항목 `i:` | `f"{item_id} {status or '-'} {title}"`, 200자에서 자름 |
| JSON 필드(`format='json'`) | 객체면 `"json keys=" + ",".join(sorted(keys))`(120자에서 자름), 배열이면 `f"json list n={len}"` |
| 실행 `r:` | `f"{run_id} {run_type} {result}"` |
| 서술 필드(`format='md'`) | `entity_summary`에 저장된 에이전트 요약. digest가 같으면 `fresh`, 다르면 `stale`, 없으면 `none` |

- `put`의 `sum`: 필드 쓰기가 성공한 뒤 `tok_est(sum) > 60`이면 `SUMMARY_TOO_LONG`(전체 취소). 민감 정보 검사 후 `entity_summary`에 `INSERT OR REPLACE`(`author_type='llm'`, `source_digest`=새 dg).
- `ctx`·`get level=1`은 구조 값은 `template`, 서술 필드는 `lookup` 결과를 원본 형식(`sum=…`, `sum=none`, `sum=stale`)으로 쓴다.

---

## C-23 카탈로그 JSON (D2, D12)

`catalogue.py`:

```python
VIEW_KINDS = {"A01", "D08", "F06", "A03"}; AUTO_KINDS = {"V05"}
RUN_FILES = ("evidence.json", "results.json", "run.json", "report.json", "findings.json")
PRIMARY_ALIASES = {"리소스 가용성 계획": "B03", "작업 환경": "G01", "제품 성공 기준": "C01", "환경별 설정 비교표": "S04"}
def storage_class(kind: dict) -> str: ...      # 원본 load_catalogue.storage_class와 같은 판정
def load(con, catalogue_path: Path, item_types_path: Path, actor: Actor | None) -> dict: ...
```

- 카탈로그 파일 구조는 원본 YAML과 같고 JSON으로 쓴다(최상위 `standard_version`, `schema_version`, `groups`, `facts`, `document_types`). `required_fields`·`optional_fields` 값은 설명 문자열 또는 `{"description": str, "extract_rel": {"rel": str, "from_prefix": str, "to_prefix": str}}`.
- `item_types.json`: `[{"item_type", "id_prefix", "defined_by_kind", "defined_by_field", "description"}]`. 원본 시드 39행을 옮기고, `defined_by_field`는 설명 괄호 안의 `<defined_by_kind>.<field>` 첫 항목에서 가져온다(없으면 `null`).
- `load` 반환: `{"catalogue_version", "kinds", "fields", "links", "aliases", "item_types", "item_types_skipped"}` 개수.
- 적재된 종류를 참조하지 않는 항목 종류는 건너뛴다(개수만 보고).
- 다시 적재할 때 값이 있는 종류·필드가 새 카탈로그에서 빠지면 `CATALOGUE_INCOMPATIBLE`(O16 미결, 기본값 = 거부). 그 밖은 추가·갱신.
- 별칭 중복은 `PRIMARY_ALIASES`의 종류만 `is_primary=1`, 나머지 `0`.

---

## C-24 CLI

`cli.py`: `def main(argv: list[str] | None = None, *, interactive: bool | None = None) -> int`. `interactive`가 `None`이면 TTY 여부로 정한다. 결과는 stdout, 오류는 stderr에 `format_error`, 반환값은 종료 코드.

전역 옵션: `--db PATH`, `--actor ID`, `--work WORK-ID`, `--session S`, `--model M`.

| 명령 | 인자 | 행위자 | 비고 |
|---|---|---|---|
| `init` | `--project <slug> --name <이름> --owner <u-…> --owner-name <이름> [--catalogue PATH] [--dir PATH]` | 없음 | `--dir`(기본 cwd)에 `.docdb/`, `.docdb/.gitignore`(내용 `*` 한 줄), 전체 마이그레이션, 카탈로그 적재, 기본 행위자 등록. 출력 `OK init project=<slug> db=<path>`. DB가 있으면 `DB_EXISTS`, slug 형식(C-18) 위반은 `BAD_ARGS`, `--owner`가 사람 형식이 아니면 `ID_FORMAT` |
| `migrate` | — | 아무나 | 백업 후 `apply_pending` |
| `backup` | `[--reason R]` | 아무나 | 기본 reason `manual` |
| `restore` | `<file>` | 사람·TTY | 백업 후 교체 |
| `actor add` / `actor list` | `<id> --type T --name N` / — | 사람·TTY / 아무나 | |
| `catalogue load` | `<json> [--item-types PATH]` | 사람·TTY | |
| `fact list` / `fact set` | — / `<cap.key> <값> --reason R` (값은 `true`, `false`, `unknown` 중 하나) | 아무나 / 사람·TTY | |
| `applicability` | — | 아무나 | `recompute` 후 개수 출력 |
| `ctx` `get` `find` `trace` `since` `put` | 원본 `docctl_llm.py` 인자 그대로, `put`에 `[--reason R]` | 아무나 | MCP와 같은 결과 |
| `link review` / `confirm` / `reject` / `remove` | `[--work W]` / `<id...>` / `<id> --why` / `<id> --why` | 아무나 / 사람·TTY | |
| `work status` | `<WORK-ID> <status>` | C-18 | |
| `handoff ack` | `<HO-ID> --baseline REF [--evidence REF ...] [--reject]` | 사람·TTY | |
| `run ingest` | `<file>` 또는 `-`(표준 입력) | 아무나 | 결과 `OK r:<RUN-ID>` |
| `import scan` / `next` / `label` / `report` | `<dir> --owner <u-…> [--export-policy P]` / `[--budget N]` / `<JSON>` 또는 `@<file>` / — | 아무나 | |
| `policy allow-secret` | `<handle> --reason R --days N` | 사람·TTY | 출력 `OK <exception_id>` |
| `verify-history` | `[--entity TYPE ID]` | 아무나 | 문제 없으면 `OK history`, 있으면 줄마다 출력 후 종료 코드 1 |

`setup`과 `mcp`는 `docdb_launch.py`의 명령이며 `cli.py`에 없다.

### `docdb_launch.py`

```text
python docdb_launch.py setup [--remove]
python docdb_launch.py mcp
python docdb_launch.py cli <docctl 인자...>
```

- 첫 단계: `sys.version_info < (3, 10)`이면 `PY_TOO_OLD`. 파일 전체가 오래된 Python에서도 **파싱**되어야 이 검사가 실행되므로, `docdb_launch.py`는 Python 3.6 문법만 쓴다(금지: `match`, `:=`, 위치 전용 인자 `/`, 괄호로 묶은 `with`, 함수 정의 시 평가되는 `X | Y` 타입 표기). `/usr/bin/python3.6 docdb_launch.py cli` 실행 시 종료 코드 2와 `ERROR PY_TOO_OLD`가 나와야 한다.
- 외부 명령은 모듈 수준 함수 `_run(cmd: list) -> None`(내부 `subprocess.run(cmd, check=True)`)으로만 실행한다(테스트가 바꿔 끼움).
- `setup`: `python -m venv <venv_dir>` → `<venv_python> -m pip install -c constraints.txt pysqlite3 mcp` → 스탬프 쓰기. `--remove`는 venv 폴더 삭제(백업 폴더는 지우지 않음).
- `mcp`·`cli`: 스탬프 없으면 `NO_VENV`, 버전 불일치면 `VENV_MISMATCH` → `sysconfig.get_paths(scheme=<"nt" if os.name == "nt" else "posix_prefix">, vars={"base": venv, "platbase": venv})`의 `purelib`·`platlib`를 `site.addsitedir`(scheme을 명시하는 이유: Debian 계열 Python은 기본 scheme이 `posix_local`이라 venv 경로가 틀어짐. RHEL은 `platlib`가 `lib64`이며 venv의 `lib64` 링크로 해결됨 — 2026-10-13 개발 서버에서 확인) → `addsitedir`로 새로 더해진 경로를 원래 순서대로 `sys.path` 맨 앞으로 옮긴다(`addsitedir`는 뒤에 붙이므로 사용자·시스템 site-packages의 오래된 패키지가 venv 것을 가린다. 2026-10-13 개발 서버에서 `~/.local`의 `typing_extensions` 때문에 `import mcp`가 실패하는 것을 재현) → `src`를 `sys.path` 맨 앞에 추가 → `import pysqlite3`, `import mcp`(실패 시 `DEP_MISSING`) → `docdb.mcp_server.main()` 또는 `docdb.cli.main(argv)`.
- `mcp`만: 서버를 시작하기 전에 `DOCDB_START_DIR`가 없거나 빈 값이고 `PWD`가 비어 있지 않으면 `os.environ["DOCDB_START_DIR"] = os.environ["PWD"]`로 둔다. 이미 있으면 덮어쓰지 않는다. `cli`에는 적용하지 않는다. 이유: Codex는 MCP 서버를 플러그인 루트에서 실행하고 환경 변수 치환이 없어 프로젝트 폴더는 `env_vars`로 전달한 `PWD`로만 알 수 있다(T00b 실측, D26).
- 오류는 stderr에 `format_error` 형식, 종료 코드는 C-05.

---

## C-25 MCP 서버

`mcp_server.py`:

```python
INSTRUCTIONS: str      # 원본 docdb_mcp.py의 INSTRUCTIONS 문자열 그대로
def build_server() -> Any: ...
def main() -> None: ...
```

- 도구 이름·인자·기본값·설명은 원본 `docdb_mcp.py`와 같다(O21 미결, 기본값 = 원본). 예외는 `docdb_put` 설명 하나이며 아래 문자열로 바꾼다:
  `"쓰기(원자적, 본문 미반환). 필드 f: set|append|replace_section(text, expect, anchor, state, why, sum). 항목 i: upsert. 관계 link(from, rel, to, why)·unlink(rel_id). 작업 w: work·handoff."`
- 원본처럼 `structured_output=False`로 등록하고 text 한 블록만 반환한다. mcp 1.x/2.x import 분기도 원본 그대로 둔다.
- 각 도구 호출 시작에서 `DOCDB_ACTOR`로 `resolve_actor` → `require_ai`를 실행하고, 실패하면 그 호출은 `format_error` 문자열을 반환한다. DB가 없거나 마이그레이션이 필요해도 서버는 시작하고, 각 도구 호출이 `format_error` 문자열(예: `ERROR NO_DB docctl init 실행 필요`)을 반환한다.
- 도구 호출마다 `open_project_db`로 연결하고 끝나면 닫는다(원본과 같음). `DOCDB_WORK`, `DOCDB_SESSION`, `DOCDB_MODEL`을 `DocDB`에 넘긴다.
- stdout에는 MCP 프로토콜 외에 아무것도 쓰지 않는다. 로그는 stderr.

---

## C-26 플러그인 파일

### `.claude-plugin/plugin.json`

[../06-runtime-plugin.md](../06-runtime-plugin.md) 6.2절의 JSON에 `mcpServers.docdb.env`로 `"DOCDB_ACTOR": "ai-claude"`를 더한 것. `version`은 `__version__`과 같게 한다.

### `.codex-plugin/plugin.json`, `codex.mcp.json`

T00b 실측(2026-10-13, codex-cli 0.162.1, [../08-verification.md](../08-verification.md) 3.1절)으로 채웠다. Windows 명령은 T00c 결과로 보완한다.

`.codex-plugin/plugin.json`:

```json
{
  "name": "docdb",
  "version": "0.1.0",
  "description": "Project document DB with token-minimal MCP tools.",
  "author": { "name": "PMT" },
  "skills": "./skills/",
  "mcpServers": "./codex.mcp.json"
}
```

`codex.mcp.json`:

```json
{
  "mcpServers": {
    "docdb": {
      "command": "python3",
      "args": ["./src/docdb_launch.py", "mcp"],
      "cwd": ".",
      "env_vars": ["PWD", "DOCDB_HOME", "XDG_DATA_HOME"],
      "env": { "DOCDB_ACTOR": "ai-codex" }
    }
  }
}
```

- `"cwd": "."`는 설치된 플러그인 루트로 풀린다. 이것이 없으면 작업 폴더가 프로젝트 폴더가 되어 상대 경로 `args`가 실패한다. `args`·`env` 안의 `${PLUGIN_ROOT}`는 치환되지 않는다(문자 그대로 전달).
- Codex는 MCP 서버에 `HOME`, `LANG`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`만 넘긴다. 그 밖의 변수는 `env_vars`에 적어야 전달된다. 프로젝트 폴더는 `PWD`로 받아 부트스트랩이 `DOCDB_START_DIR`로 옮긴다(C-24).
- `version`은 `__version__`과 같게 한다. `DOCDB_ACTOR`는 `"ai-codex"`.

### `bin/docctl`

POSIX sh. `DOCDB_HOME` → (macOS면 `~/Library/Application Support/docdb`) → `${XDG_DATA_HOME:-$HOME/.local/share}/docdb` 순으로 데이터 폴더를 정하고, `<data>/venv/bin/python`이 있으면 `PYTHONPATH=<plugin>/src` 상태로 `exec "$py" -m docdb "$@"`. 없으면 stderr에 `ERROR NO_VENV …` 한 줄과 종료 코드 2. 플러그인 경로는 `$(dirname "$0")/..`로 구한다.

### `skills/docdb/SKILL.md`

필수 절(이 순서): 언제 쓰나 / 설치(`setup`, `init`) / 사용 규칙(원본 토큰 명세 부록 A 문장 그대로) / 관계 제안 규칙(`link`는 근거 `why` 필수, 확정은 사람) / 작업·인계 / import 절차(05 문서 흐름, 큰 import는 서브에이전트로) / 오류 코드 대응표(C-05에서 에이전트가 만날 코드만). 2,000자 이내의 앞부분에 "언제 쓰나"와 핵심 규칙을 둔다.

### 마켓플레이스

`.claude-plugin/marketplace.json`의 `plugins`에 `{"name": "docdb", "source": "./docdb", "description": "Project document DB with token-minimal MCP tools.", "version": "0.1.0", "category": "Productivity", "strict": true}`. `.agents/plugins/marketplace.json`에는 기존 항목 형식을 따라 `docdb` 추가.

---

## C-27 테스트 하네스

- `pytest.ini`: `[pytest]` / `testpaths = tests` / `pythonpath = src` / `addopts = -q`.
- 개발 venv: `docdb/.venv`(저장소 `.gitignore`에 이미 `.venv/` 있음). 만들기: `uv venv docdb/.venv --python <버전>` → `uv pip install --python docdb/.venv/bin/python -c docdb/constraints-dev.txt pytest pysqlite3 mcp`.
- 테스트 실행: `cd docdb && .venv/bin/python -m pytest`.
- 네트워크 없이 통과해야 한다. 실제 `pip install`은 테스트하지 않는다(`setup`은 `subprocess` 호출부를 주입해서 검증).

`tests/conftest.py`가 제공하는 픽스처(이 이름 그대로). 괄호 안은 도입 작업이며, 그 전 작업의 테스트는 해당 픽스처를 쓰지 않는다:

| 픽스처 | 내용 |
|---|---|
| `frozen_now` (T01·T02) | `docdb.clock.now_iso` → `"2026-10-13T00:00:00+00:00"`, `utc_year` → 2026으로 monkeypatch |
| `plugin_root` (T01) | `docdb/` 경로 |
| `schema_con` (T05) | `tmp_path/"s.sqlite"`에 `connect(create=True)` 후 `apply_pending`한 연결(카탈로그 없음) |
| `starter_paths` (T10) | `(catalogue/starter.json, catalogue/item_types.json)` |
| `mini_paths` (T10) | `(tests/fixtures/mini_catalogue.json, catalogue/item_types.json)` |
| `project_db` (T11) | `tmp_path`에서 `cli.main(["init", "--project", "t", "--name", "테스트", "--owner", "u-tester", "--owner-name", "Tester", "--dir", str(tmp_path)])` 실행 후 DB 경로 반환 |
| `mini_db` (T11) | 위와 같되 `--catalogue <mini_catalogue.json>` |
| `con` (T11) | `db.connect(project_db)` 연결(테스트 끝에 닫음) |
| `human` / `agent` / `ci` (T11) | `Actor("u-tester","human")` / `Actor("ai-claude","ai")` / `Actor("team-ci","team")` |
| `run_cli` (T11) | 서명은 표 아래 코드 블록. `monkeypatch.setenv`로 env 적용, `capsys`로 stdout·stderr 수집 |


`run_cli` 서명:

```python
def run_cli(args: list[str], interactive: bool = False, env: dict[str, str] | None = None) -> tuple[int, str, str]: ...  # (종료 코드, stdout, stderr)
```

- `tests/helpers.py`(T06): `seed_min(con)` — T06 작업 절에 적힌 최소 카탈로그·프로젝트·행위자를 SQL로 넣는다. T07~T10 단위 테스트가 쓴다.
- 테스트 건너뛰기는 두 경우만 허용한다: 필요한 인터프리터가 없음(예: `/usr/bin/python3.6`), 필요한 OS가 아님. `pytest.skip("<이유>")`로 이유를 적는다.
- `tests/fixtures/mini_catalogue.json`: 원본 `test_token.py`의 `mini_catalogue()`가 만드는 데이터를 JSON으로 그대로 옮긴 것.
- `tests/legacy/`: 원본 테스트 3종(`test_scenarios.py` 33건, `test_token.py` 28건, `audit.py` 94항목)을 pytest로 옮긴 것. 원본의 검사 조건과 기대 수치를 바꾸지 않는다. 바뀌는 것은 다음뿐이다: DB 생성은 `mini_db` 픽스처, SQL 직접 실행은 `con.execute`, ID 형식은 C-10(`RUN-0001` → `RUN-2026-0001` 등), 관계를 SQL로 직접 넣는 곳은 `source='human', proposed_by='u-jjm'` 추가(0003 트리거 때문), "194종 집계"는 "적재한 종류 수와 같음".
- `tools/matrix.sh`: Python 3.10~3.14 각각에 대해 `uv venv` → 설치 → pytest를 실행하고 버전별 통과·실패 요약을 출력한다.
