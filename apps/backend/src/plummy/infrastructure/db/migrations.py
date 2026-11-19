"""Alembic metadata needed at runtime (readiness: are all migrations applied?)."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from plummy.core.version import BACKEND_ROOT

ALEMBIC_DIR = BACKEND_ROOT / "alembic"


def get_expected_heads(script_location: Path = ALEMBIC_DIR) -> frozenset[str]:
    """Head revisions of the migration scripts shipped with this build."""
    config = Config()
    config.set_main_option("script_location", str(script_location))
    heads = ScriptDirectory.from_config(config).get_heads()
    if not heads:
        raise RuntimeError(f"no Alembic revisions found in {script_location}")
    return frozenset(heads)


RUNTIME_INDEX_PREFIX = "ix_embeddings_hnsw_"


def include_object(obj: object, name: str | None, type_: str, reflected: bool, compare_to: object) -> bool:
    """Alembic filter: the HNSW index of each embedding profile is created at runtime (ADR-017 §2)."""
    return not (type_ == "index" and reflected and name is not None and name.startswith(RUNTIME_INDEX_PREFIX))
