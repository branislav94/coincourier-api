-- Fresh-start dry run.
-- Replace this value before running. This script is read-only.
SET @fresh_start_after_utc = 'YYYY-MM-DD HH:MM:SS';

-- 1) Selected but unprocessed raw rows.
SELECT COUNT(*) AS selected_unprocessed_total
FROM cryptonewsapi
WHERE chosen_for_publish = 1
  AND processed = 0;

SELECT id, news_url, title, source_name, insertDate, selected_at, scheduled_for, processed
FROM cryptonewsapi
WHERE chosen_for_publish = 1
  AND processed = 0
ORDER BY selected_at DESC, scheduled_for ASC
LIMIT 25;

-- 2) Selected/scheduled raw rows before cutoff.
SELECT COUNT(*) AS selected_or_scheduled_before_cutoff
FROM cryptonewsapi
WHERE insertDate < @fresh_start_after_utc
  AND (
    chosen_for_publish = 1
    OR selected_at IS NOT NULL
    OR scheduled_for IS NOT NULL
  );

SELECT id, news_url, title, source_name, insertDate, selected_at, scheduled_for, processed
FROM cryptonewsapi
WHERE insertDate < @fresh_start_after_utc
  AND (
    chosen_for_publish = 1
    OR selected_at IS NOT NULL
    OR scheduled_for IS NOT NULL
  )
ORDER BY insertDate DESC
LIMIT 25;

-- 3) Processed raw rows without a published rich row.
SELECT COUNT(*) AS processed_raw_without_published_rich
FROM cryptonewsapi c
LEFT JOIN rich_crpytonews r
  ON r.news_url = c.news_url
 AND r.published = 1
WHERE c.processed = 1
  AND r.id IS NULL;

SELECT c.id, c.news_url, c.title, c.insertDate, c.selected_at, c.scheduled_for, c.processed
FROM cryptonewsapi c
LEFT JOIN rich_crpytonews r
  ON r.news_url = c.news_url
 AND r.published = 1
WHERE c.processed = 1
  AND r.id IS NULL
ORDER BY c.insertDate DESC
LIMIT 25;

-- 4) Unpublished rich rows.
SELECT COUNT(*) AS unpublished_rich_total
FROM rich_crpytonews
WHERE published = 0;

SELECT id, news_url, title, source_name, publish_date, insertDate, published
FROM rich_crpytonews
WHERE published = 0
ORDER BY insertDate DESC
LIMIT 25;

-- 5) Publish-ready rows by the primary publisher query.
SELECT COUNT(*) AS publish_ready_total
FROM rich_crpytonews r
JOIN cryptonewsapi c ON c.news_url = r.news_url
WHERE r.published = 0
  AND c.chosen_for_publish = 1
  AND c.scheduled_for IS NOT NULL
  AND c.scheduled_for <= UTC_TIMESTAMP();

SELECT r.id AS rich_id, c.id AS raw_id, r.news_url, r.title, c.insertDate,
       c.selected_at, c.scheduled_for, r.published
FROM rich_crpytonews r
JOIN cryptonewsapi c ON c.news_url = r.news_url
WHERE r.published = 0
  AND c.chosen_for_publish = 1
  AND c.scheduled_for IS NOT NULL
  AND c.scheduled_for <= UTC_TIMESTAMP()
ORDER BY c.scheduled_for ASC
LIMIT 25;

-- 6) Stale rows before cutoff that the apply script would disable/delete.
SELECT COUNT(*) AS old_raw_queue_rows_to_disable
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
  );

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
LIMIT 25;

SELECT COUNT(*) AS old_unpublished_rich_rows_to_delete
FROM rich_crpytonews r
JOIN cryptonewsapi c ON c.news_url = r.news_url
WHERE r.published = 0
  AND c.insertDate < @fresh_start_after_utc;

SELECT r.id AS rich_id, c.id AS raw_id, r.news_url, r.title, c.insertDate,
       r.publish_date, r.insertDate AS rich_insertDate, r.published
FROM rich_crpytonews r
JOIN cryptonewsapi c ON c.news_url = r.news_url
WHERE r.published = 0
  AND c.insertDate < @fresh_start_after_utc
ORDER BY c.insertDate DESC
LIMIT 25;

-- 7) Rows that could be reached by publisher fallback.
SELECT COUNT(*) AS publisher_fallback_reachable_total
FROM rich_crpytonews r
LEFT JOIN cryptonewsapi c ON c.news_url = r.news_url
WHERE r.published = 0;

SELECT r.id AS rich_id, c.id AS raw_id, r.news_url, r.title,
       c.insertDate AS raw_insertDate, c.chosen_for_publish,
       c.scheduled_for, c.final_importance, r.publish_date
FROM rich_crpytonews r
LEFT JOIN cryptonewsapi c ON c.news_url = r.news_url
WHERE r.published = 0
ORDER BY COALESCE(c.is_breaking, 0) DESC,
         COALESCE(c.final_importance, 0) DESC,
         r.publish_date DESC
LIMIT 25;
