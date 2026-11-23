from __future__ import annotations

from pathlib import Path
from typing import Optional

from ledger.check import SourceUnavailable
from ledger.spec import TEMPLATE_FOR

_PLANE_TEMPLATES = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "forge-ledger"
    / "skills"
    / "ledger-plan"
    / "templates"
    / "plane"
)


class DictSource:
    """메모리 딕셔너리 기반 단위 문서 원천."""

    def __init__(self, docs: dict[tuple[str, str], str]) -> None:
        self._docs = dict(docs)

    def get_container_html(self, project_key: str, container: str) -> Optional[str]:
        return self._docs.get((project_key, container))

    @classmethod
    def from_templates(cls, project_key: str) -> DictSource:
        docs = {
            (project_key, container): (_PLANE_TEMPLATES / filename).read_text(encoding="utf-8")
            for container, filename in TEMPLATE_FOR.items()
        }
        return cls(docs)


class RaisingSource:
    """항상 SourceUnavailable을 던지는 테스트용 원천."""

    def get_container_html(self, project_key: str, container: str) -> Optional[str]:
        raise SourceUnavailable(f"unavailable: {project_key}/{container}")
