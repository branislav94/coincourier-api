-- Additive Phase 2 durable processing/publication state for MariaDB 10.4+.
-- Safe to rerun; this script does not remove or rename legacy columns.

ALTER TABLE cryptonewsapi
    ADD COLUMN IF NOT EXISTS processing_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS processing_claim_token CHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS processing_claimed_at DATETIME NULL,
    ADD COLUMN IF NOT EXISTS processing_attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS processing_last_error VARCHAR(500) NULL;

ALTER TABLE rich_crpytonews
    ADD COLUMN IF NOT EXISTS raw_article_id INT NULL,
    ADD COLUMN IF NOT EXISTS publish_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS publish_claim_token CHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS publish_claimed_at DATETIME NULL,
    ADD COLUMN IF NOT EXISTS publish_attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS publish_last_error VARCHAR(500) NULL,
    ADD COLUMN IF NOT EXISTS publication_key VARCHAR(191) NULL,
    ADD COLUMN IF NOT EXISTS wp_post_id BIGINT UNSIGNED NULL,
    ADD COLUMN IF NOT EXISTS wp_media_id BIGINT UNSIGNED NULL,
    ADD COLUMN IF NOT EXISTS wp_media_metadata_json LONGTEXT NULL,
    ADD COLUMN IF NOT EXISTS wp_post_url VARCHAR(512) NULL,
    ADD COLUMN IF NOT EXISTS wp_post_created_at DATETIME NULL,
    ADD COLUMN IF NOT EXISTS published_at DATETIME NULL;

-- Preserve the existing booleans as compatibility state for historical rows.
UPDATE cryptonewsapi
SET processing_status = 'completed'
WHERE processed = 1 AND processing_status <> 'completed';

UPDATE rich_crpytonews
SET publish_status = 'published'
WHERE published = 1 AND publish_status <> 'published';

-- URL is the existing unique relationship; preflight 001 proves it is unambiguous.
UPDATE rich_crpytonews r
JOIN cryptonewsapi c ON c.news_url = r.news_url
SET r.raw_article_id = COALESCE(r.raw_article_id, c.id);

-- Durable IDs are primary. The hash fallback only covers a pre-existing orphan.
UPDATE rich_crpytonews
SET publication_key = COALESCE(
    publication_key,
    CASE
        WHEN raw_article_id IS NOT NULL
            THEN CONCAT('coincourier:', id, ':', raw_article_id)
        ELSE CONCAT(
            'coincourier:rich:', id, ':source:',
            SHA2(LOWER(TRIM(COALESCE(news_url, ''))), 256)
        )
    END
);
