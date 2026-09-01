-- Run after 002 and before creating Phase 2 unique indexes.
-- Every query must return zero rows.

SELECT publication_key, COUNT(*) AS duplicate_count
FROM rich_crpytonews
WHERE publication_key IS NOT NULL
GROUP BY publication_key
HAVING COUNT(*) > 1;

SELECT raw_article_id, COUNT(*) AS rich_row_count
FROM rich_crpytonews
WHERE raw_article_id IS NOT NULL
GROUP BY raw_article_id
HAVING COUNT(*) > 1;

SELECT wp_post_id, COUNT(*) AS local_reference_count
FROM rich_crpytonews
WHERE wp_post_id IS NOT NULL
GROUP BY wp_post_id
HAVING COUNT(*) > 1;
