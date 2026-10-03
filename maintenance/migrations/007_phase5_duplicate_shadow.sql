-- Additive Phase 5 deterministic duplicate audit state for MariaDB 10.4+.
-- Safe to rerun after the 006 preflight has established table ownership.

CREATE TABLE IF NOT EXISTS duplicate_assessments (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    article_id INT NOT NULL,
    candidate_article_id INT NOT NULL,
    assessment_type VARCHAR(32) NOT NULL,
    same_provider_article_id TINYINT(1) NOT NULL DEFAULT 0,
    same_event_id TINYINT(1) NOT NULL DEFAULT 0,
    same_canonical_url TINYINT(1) NOT NULL DEFAULT 0,
    same_content_hash TINYINT(1) NOT NULL DEFAULT 0,
    title_token_jaccard DECIMAL(6,5) NOT NULL DEFAULT 0,
    publication_distance_hours DECIMAL(10,3) NULL,
    shared_entities_json LONGTEXT NOT NULL,
    shared_dates_json LONGTEXT NOT NULL,
    shared_numbers_json LONGTEXT NOT NULL,
    reason_json LONGTEXT NOT NULL,
    policy_version VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_duplicate_assessment_pair_policy
        (article_id, candidate_article_id, policy_version),
    KEY idx_duplicate_assessment_article_created (article_id, created_at),
    KEY idx_duplicate_assessment_candidate_created (candidate_article_id, created_at),
    CONSTRAINT chk_duplicate_assessment_distinct_articles
        CHECK (article_id <> candidate_article_id)
);
