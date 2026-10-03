-- Disposable integration-test baseline only.
--
-- Repository evidence:
--   GetNewsAPI/crypto_news_db.sql (MariaDB 10.4.32, generated 2025-04-15)
-- supplies the original column types, InnoDB engine, utf8mb4_general_ci
-- collation, primary keys, and unique news_url keys. The dump's historical
-- INSERT rows are intentionally not imported.
--
-- Test adaptation:
--   The dump predates current identity, scoring, scheduling, and rich SEO
-- fields. The extra columns below are the minimum pre-Phase-2 fields used by
-- current committed SQL and Phase 5 preflight. This baseline deliberately has
-- no Phase 2 columns and no duplicate_assessments table.

DROP TABLE IF EXISTS duplicate_assessments;
DROP TABLE IF EXISTS rich_crpytonews;
DROP TABLE IF EXISTS cryptonewsapi;

CREATE TABLE cryptonewsapi (
    id INT NOT NULL AUTO_INCREMENT,
    news_url VARCHAR(512) NOT NULL,
    canonical_url VARCHAR(512) NULL,
    title VARCHAR(255) NOT NULL,
    full_text TEXT NULL,
    publish_date DATETIME NULL,
    source_name VARCHAR(255) NULL,
    topics VARCHAR(255) NULL,
    sentiment FLOAT NULL,
    type VARCHAR(100) NULL,
    tickers VARCHAR(255) NULL,
    image_url VARCHAR(512) NULL,
    insertDate DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published TINYINT(1) NOT NULL DEFAULT 0,
    processed TINYINT(1) NOT NULL DEFAULT 0,
    news_id VARCHAR(255) NULL,
    event_id VARCHAR(255) NULL,
    rank_score FLOAT NULL,
    title_hash CHAR(64) NULL,
    fetch_batch_id VARCHAR(64) NULL,
    gpt_importance FLOAT NULL,
    recency_score FLOAT NULL,
    source_weight FLOAT NULL,
    final_importance FLOAT NULL,
    is_breaking TINYINT(1) NOT NULL DEFAULT 0,
    chosen_for_publish TINYINT(1) NOT NULL DEFAULT 0,
    selected_at DATETIME NULL,
    scheduled_for DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_cryptonewsapi_news_url (news_url)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE rich_crpytonews (
    id INT NOT NULL AUTO_INCREMENT,
    news_url VARCHAR(512) NOT NULL,
    title VARCHAR(255) NOT NULL,
    full_text TEXT NULL,
    publish_date DATETIME NULL,
    source_name VARCHAR(255) NULL,
    topics VARCHAR(255) NULL,
    category VARCHAR(255) NULL,
    hashtags VARCHAR(512) NULL,
    sentiment FLOAT NULL,
    type VARCHAR(100) NULL,
    tickers VARCHAR(255) NULL,
    image_url VARCHAR(512) NULL,
    seo_focus VARCHAR(255) NULL,
    seo_slug VARCHAR(255) NULL,
    seo_meta VARCHAR(500) NULL,
    insertDate DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published TINYINT(1) NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uq_rich_crpytonews_news_url (news_url)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
