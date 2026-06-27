-- Fresh-start cleanup apply script.
-- Review maintenance/sql/fresh_start_dry_run.sql first.
-- Replace this value before running.
SET @fresh_start_after_utc = 'YYYY-MM-DD HH:MM:SS';

START TRANSACTION;

-- Preview raw queue rows that will be disabled.
SELECT c.id, c.news_url, c.title, c.source_name, c.insertDate,
       c.selected_at, c.scheduled_for, c.processed
FROM cryptonewsapi c
LEFT JOIN rich_crpytonews published_r
  ON published_r.news_url = c.news_url
 AND published_r.published = 1
WHERE c.insertDate < @fresh_start_after_utc
  AND published_r.id IS NULL
  AND (
    c.chosen_for_publish = 1
    OR c.selected_at IS NOT NULL
    OR c.scheduled_for IS NOT NULL
  )
ORDER BY c.insertDate DESC
LIMIT 100;

-- Preview old unpublished rich rows that will be deleted.
SELECT r.id AS rich_id, c.id AS raw_id, r.news_url, r.title, c.insertDate,
       r.publish_date, r.insertDate AS rich_insertDate, r.published
FROM rich_crpytonews r
JOIN cryptonewsapi c ON c.news_url = r.news_url
WHERE r.published = 0
  AND c.insertDate < @fresh_start_after_utc
ORDER BY c.insertDate DESC
LIMIT 100;

-- Disable old raw rows still queued in the app pipeline.
-- This does not set processed = 1.
UPDATE cryptonewsapi c
LEFT JOIN rich_crpytonews published_r
  ON published_r.news_url = c.news_url
 AND published_r.published = 1
SET c.chosen_for_publish = 0,
    c.selected_at = NULL,
    c.scheduled_for = NULL
WHERE c.insertDate < @fresh_start_after_utc
  AND published_r.id IS NULL
  AND (
    c.chosen_for_publish = 1
    OR c.selected_at IS NOT NULL
    OR c.scheduled_for IS NOT NULL
  );

SELECT ROW_COUNT() AS raw_queue_rows_disabled;

-- Delete only old unpublished rich rows tied to old raw rows.
-- This does not touch rich_crpytonews.published = 1 rows or WordPress posts.
DELETE r
FROM rich_crpytonews r
JOIN cryptonewsapi c ON c.news_url = r.news_url
WHERE r.published = 0
  AND c.insertDate < @fresh_start_after_utc;

SELECT ROW_COUNT() AS unpublished_rich_rows_deleted;

-- Safety default: rollback. After reviewing all output, replace ROLLBACK with COMMIT.
ROLLBACK;
-- COMMIT;
