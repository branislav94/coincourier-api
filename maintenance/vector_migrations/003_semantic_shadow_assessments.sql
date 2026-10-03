-- Phase 6C2A durable semantic evidence for the separate MariaDB 11.8 vector DB.
-- Safe to rerun. This table stores bounded retrieval evidence, never decisions.

CREATE TABLE IF NOT EXISTS semantic_shadow_assessments (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    query_source_article_id INT NOT NULL,
    query_document_id BIGINT UNSIGNED NULL,
    query_document_identity BIGINT UNSIGNED
        AS (IFNULL(query_document_id, 0)) PERSISTENT,
    embedding_version VARCHAR(191) NOT NULL,
    semantic_version VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    reason VARCHAR(191) NULL,
    lookback_hours SMALLINT UNSIGNED NOT NULL,
    requested_top_k SMALLINT UNSIGNED NOT NULL,
    query_chunks_available INT UNSIGNED NOT NULL DEFAULT 0,
    query_chunks_considered SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    candidate_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    best_native_distance DOUBLE NULL,
    evidence_json LONGTEXT NOT NULL,
    error_type VARCHAR(128) NULL,
    safe_error VARCHAR(500) NULL,
    evaluation_count INT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_semantic_shadow_identity
        (query_source_article_id, query_document_identity,
         embedding_version, semantic_version),
    KEY idx_semantic_shadow_source_version
        (query_source_article_id, semantic_version, updated_at),
    KEY idx_semantic_shadow_document_version
        (query_document_id, embedding_version, semantic_version),
    KEY idx_semantic_shadow_status_updated (status, updated_at),
    CONSTRAINT fk_semantic_shadow_query_document
        FOREIGN KEY (query_document_id) REFERENCES vector_documents (id)
        ON DELETE CASCADE,
    CONSTRAINT chk_semantic_shadow_source_id
        CHECK (query_source_article_id > 0),
    CONSTRAINT chk_semantic_shadow_status
        CHECK (status IN ('disabled', 'not_ready', 'no_candidates', 'retrieved', 'error')),
    CONSTRAINT chk_semantic_shadow_bounds
        CHECK (
            lookback_hours BETWEEN 1 AND 8760
            AND requested_top_k BETWEEN 1 AND 20
            AND query_chunks_considered <= 8
            AND candidate_count <= 20
        ),
    CONSTRAINT chk_semantic_shadow_evidence_json
        CHECK (
            JSON_VALID(evidence_json)
            AND JSON_TYPE(evidence_json) = 'ARRAY'
            AND JSON_LENGTH(evidence_json) = candidate_count
        ),
    CONSTRAINT chk_semantic_shadow_result_shape
        CHECK (
            (status = 'retrieved' AND candidate_count > 0
                AND best_native_distance IS NOT NULL)
            OR
            (status <> 'retrieved' AND candidate_count = 0
                AND best_native_distance IS NULL)
        ),
    CONSTRAINT chk_semantic_shadow_error_shape
        CHECK (
            (status = 'error' AND error_type IS NOT NULL AND safe_error IS NOT NULL)
            OR
            (status <> 'error' AND error_type IS NULL AND safe_error IS NULL)
        )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
