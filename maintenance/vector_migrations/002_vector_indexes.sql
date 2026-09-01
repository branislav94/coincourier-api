-- Native cosine index for Phase 6A retrieval on MariaDB 11.8.
-- Safe to rerun; MariaDB reports a duplicate-key note when the index exists.

CREATE VECTOR INDEX IF NOT EXISTS idx_vector_chunks_embedding_cosine
    ON vector_chunks (embedding) DISTANCE=cosine;
