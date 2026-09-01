-- Phase 5 preflight. Every result set must be empty before continuing.

-- Phase 5 owns this new table name; stop if an unmanaged table already uses it.
SELECT TABLE_NAME
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'duplicate_assessments';

-- Runtime identity and candidate-query columns must already exist.
SELECT required.COLUMN_NAME AS missing_column
FROM (
    SELECT 'id' AS COLUMN_NAME
    UNION ALL SELECT 'news_id'
    UNION ALL SELECT 'event_id'
    UNION ALL SELECT 'news_url'
    UNION ALL SELECT 'canonical_url'
    UNION ALL SELECT 'title'
    UNION ALL SELECT 'title_hash'
    UNION ALL SELECT 'full_text'
    UNION ALL SELECT 'publish_date'
    UNION ALL SELECT 'source_name'
    UNION ALL SELECT 'topics'
    UNION ALL SELECT 'tickers'
    UNION ALL SELECT 'processed'
    UNION ALL SELECT 'chosen_for_publish'
) required
LEFT JOIN information_schema.COLUMNS existing
  ON existing.TABLE_SCHEMA = DATABASE()
 AND existing.TABLE_NAME = 'cryptonewsapi'
 AND existing.COLUMN_NAME = required.COLUMN_NAME
WHERE existing.COLUMN_NAME IS NULL;
