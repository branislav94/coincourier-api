-- Operational rollback only: retain all additive columns and indexes.
-- Stop workers and disable both durable-state feature flags before running.

UPDATE cryptonewsapi
SET processing_status = CASE WHEN processed = 1 THEN 'completed' ELSE 'retryable' END,
    processing_claim_token = NULL,
    processing_claimed_at = NULL
WHERE processing_status = 'claimed';

UPDATE rich_crpytonews
SET publish_status = CASE WHEN published = 1 THEN 'published' ELSE 'retryable' END,
    publish_claim_token = NULL,
    publish_claimed_at = NULL
WHERE publish_status IN ('claimed', 'post_created');
