"""수용 테스트 표의 모든 행에 테스트 함수가 있는지 검사한다(핸드오프 검증용, 표준 라이브러리만).

사용: python check_rows.py <작업ID> <행 수> <테스트 파일> [<테스트 파일> ...]
예:   python check_rows.py T02 14 tests/unit/test_t02_text.py

테스트 함수 이름 규칙(00-discipline 5절 3번): test_<작업ID 소문자>_<행 번호>_<설명>
행 1..N마다 그런 함수가 하나 이상 있으면 'rows ok'를 출력하고 종료 코드 0, 아니면 빠진 행을 출력하고 1.
"""
import re
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    task, total, files = argv[0].lower(), int(argv[1]), argv[2:]
    pattern = re.compile(rf"^\s*(?:async\s+)?def\s+test_{re.escape(task)}_(\d+)_", re.M)
    found: set[int] = set()
    for f in files:
        path = Path(f)
        if not path.is_file():
            print(f"missing file: {f}")
            return 1
        found.update(int(n) for n in pattern.findall(path.read_text(encoding="utf-8")))
    missing = sorted(set(range(1, total + 1)) - found)
    if missing:
        print(f"missing rows {argv[0]}: " + ",".join(map(str, missing)))
        return 1
    print(f"rows ok {argv[0]} {total}/{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
