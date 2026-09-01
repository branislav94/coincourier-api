-- Apply only after both preflight scripts return zero rows.

CREATE INDEX IF NOT EXISTS idx_cryptonewsapi_processing_claim
    ON cryptonewsapi (processed, chosen_for_publish, processing_status, processing_claimed_at);

CREATE INDEX IF NOT EXISTS idx_rich_crpytonews_publish_claim
    ON rich_crpytonews (published, publish_status, publish_claimed_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_rich_crpytonews_raw_article_id
    ON rich_crpytonews (raw_article_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_rich_crpytonews_publication_key
    ON rich_crpytonews (publication_key);

CREATE UNIQUE INDEX IF NOT EXISTS uq_rich_crpytonews_wp_post_id
    ON rich_crpytonews (wp_post_id);
