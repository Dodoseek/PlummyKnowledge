"""Per-profile HNSW indexes built through functions of the schema owner (ADR-024, ADR-017 §2).

The services' runtime role has no DDL: ``CREATE INDEX`` needs the owner of ``embeddings``. These two
``SECURITY DEFINER`` functions are the only DDL the runtime role can trigger (``db grant-runtime`` grants
``EXECUTE`` on them and nothing else). They take a profile id only and build the statement from the stored,
CHECK-constrained profile row: the index name from the UUID, the dimensions as an integer, the operator class
from a fixed mapping. The expression and predicate match ``application/knowledge/vector_index.py`` exactly —
retrieval uses the same text (``test_indexing.py`` compares them).

A function runs in a transaction, so the build is not ``CONCURRENTLY``: inserts into ``embeddings`` wait for
it (reads do not). Indexes are built when a knowledge base starts using a profile, before its vectors arrive.
An invalid index left by an earlier concurrent build is dropped and built again. ``search_path`` includes
``public`` for the pgvector types and operator classes: nobody but the owner can create objects there
(``db grant-runtime`` revokes ``CREATE`` from ``PUBLIC``), so it cannot be used to plant a lookalike.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENSURE = r"""
CREATE FUNCTION public.plummy_ensure_vector_index(profile_id uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  dims integer;
  metric text;
  profile_status text;
  index_name text := 'ix_embeddings_hnsw_' || replace(profile_id::text, '-', '');
  valid boolean;
  kind text;
  ops text;
BEGIN
  SELECT c.dimensions, c.distance_metric, c.status INTO dims, metric, profile_status
    FROM public.embedding_provider_configs c WHERE c.id = profile_id;
  IF NOT FOUND OR profile_status = 'retired' THEN
    RETURN false;
  END IF;
  IF dims < 1 OR dims > 4000 THEN
    RAISE EXCEPTION 'profile dimensions are not indexable' USING ERRCODE = '22023';
  END IF;
  ops := CASE metric
    WHEN 'cosine' THEN 'cosine_ops' WHEN 'inner_product' THEN 'ip_ops' WHEN 'l2' THEN 'l2_ops'
  END;
  IF ops IS NULL THEN
    RAISE EXCEPTION 'unknown distance metric' USING ERRCODE = '22023';
  END IF;
  kind := CASE WHEN dims <= 2000 THEN 'vector' ELSE 'halfvec' END;
  -- Concurrent callers queue here; the second one finds the index built.
  PERFORM pg_advisory_xact_lock(hashtextextended(index_name, 0));
  SELECT i.indisvalid INTO valid FROM pg_class r JOIN pg_index i ON i.indexrelid = r.oid
    JOIN pg_namespace n ON n.oid = r.relnamespace WHERE r.relname = index_name AND n.nspname = 'public';
  IF valid THEN
    RETURN false;
  END IF;
  IF valid IS NOT NULL THEN
    EXECUTE format('DROP INDEX public.%I', index_name);
  END IF;
  EXECUTE format(
    'CREATE INDEX %I ON public.embeddings USING hnsw ((embedding::%s(%s)) %s) WHERE embedding_config_id = %L',
    index_name, kind, dims, kind || '_' || ops, profile_id
  );
  RETURN true;
END
$$
"""

DROP = r"""
CREATE FUNCTION public.plummy_drop_vector_index(profile_id uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  index_name text := 'ix_embeddings_hnsw_' || replace(profile_id::text, '-', '');
BEGIN
  IF to_regclass(format('public.%I', index_name)) IS NULL THEN
    RETURN false;
  END IF;
  EXECUTE format('DROP INDEX public.%I', index_name);
  RETURN true;
END
$$
"""


def upgrade() -> None:
    op.execute(ENSURE)
    op.execute(DROP)
    # Nobody but the owner executes them until `db grant-runtime` grants the runtime role (ADR-024 §2).
    op.execute("REVOKE ALL ON FUNCTION public.plummy_ensure_vector_index(uuid) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION public.plummy_drop_vector_index(uuid) FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.plummy_drop_vector_index(uuid)")
    op.execute("DROP FUNCTION IF EXISTS public.plummy_ensure_vector_index(uuid)")
