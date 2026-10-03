-- Phase 6A schema for the separate MariaDB 11.8 vector database only.
-- This migration must never be run against the application database.

CREATE TABLE IF NOT EXISTS vector_documents (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    document_key VARCHAR(191) NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    source_article_id INT NOT NULL,
    rich_article_id INT NULL,
    source_url VARCHAR(1024) NULL,
    title VARCHAR(512) NOT NULL,
    published_at DATETIME NULL,
    content_hash CHAR(64) NOT NULL,
    content_version VARCHAR(128) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_vector_documents_key_version (document_key, content_version),
    KEY idx_vector_documents_source (source_type, source_article_id),
    KEY idx_vector_documents_rich (rich_article_id),
    KEY idx_vector_documents_published (published_at),
    CONSTRAINT chk_vector_documents_source_type
        CHECK (source_type IN ('source_article', 'coincourier_generated')),
    CONSTRAINT chk_vector_documents_provenance
        CHECK (
            (source_type = 'source_article' AND rich_article_id IS NULL)
            OR
            (source_type = 'coincourier_generated' AND rich_article_id IS NOT NULL)
        )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS vector_chunks (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    document_id BIGINT UNSIGNED NOT NULL,
    chunk_index INT UNSIGNED NOT NULL,
    chunk_text MEDIUMTEXT NOT NULL,
    chunk_hash CHAR(64) NOT NULL,
    embedding VECTOR(1536) NOT NULL,
    embedding_model VARCHAR(191) NOT NULL,
    embedding_dimensions SMALLINT UNSIGNED NOT NULL,
    embedding_version VARCHAR(191) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_vector_chunks_position_version
        (document_id, chunk_index, embedding_version),
    UNIQUE KEY uq_vector_chunks_hash_version
        (document_id, chunk_hash, embedding_version),
    KEY idx_vector_chunks_document_version
        (document_id, embedding_version, chunk_index),
    CONSTRAINT fk_vector_chunks_document
        FOREIGN KEY (document_id) REFERENCES vector_documents (id) ON DELETE CASCADE,
    CONSTRAINT chk_vector_chunks_dimensions
        CHECK (embedding_dimensions = 1536)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS embedding_jobs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    document_id BIGINT UNSIGNED NOT NULL,
    embedding_version VARCHAR(191) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    claim_token CHAR(64) NULL,
    claimed_at DATETIME NULL,
    attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
    last_error VARCHAR(500) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_embedding_jobs_document_version (document_id, embedding_version),
    KEY idx_embedding_jobs_claim (status, claimed_at, id),
    CONSTRAINT fk_embedding_jobs_document
        FOREIGN KEY (document_id) REFERENCES vector_documents (id) ON DELETE CASCADE,
    CONSTRAINT chk_embedding_jobs_status
        CHECK (status IN ('pending', 'claimed', 'completed', 'retryable', 'failed'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
