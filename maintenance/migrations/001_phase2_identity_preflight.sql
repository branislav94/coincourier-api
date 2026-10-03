-- Phase 2 preflight against columns known to exist before the migration.
-- Every query must return zero rows before continuing.

-- A URL must identify at most one raw row.
SELECT news_url, COUNT(*) AS duplicate_count
FROM cryptonewsapi
GROUP BY news_url
HAVING COUNT(*) > 1;

-- A URL must identify at most one rich row.
SELECT news_url, COUNT(*) AS duplicate_count
FROM rich_crpytonews
GROUP BY news_url
HAVING COUNT(*) > 1;

-- Every rich row must resolve to exactly one raw row through the established URL key.
SELECT r.id AS rich_article_id, r.news_url, COUNT(c.id) AS raw_match_count
FROM rich_crpytonews r
LEFT JOIN cryptonewsapi c ON c.news_url = r.news_url
GROUP BY r.id, r.news_url
HAVING COUNT(c.id) <> 1;
