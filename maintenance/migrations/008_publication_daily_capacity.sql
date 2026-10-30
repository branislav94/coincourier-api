-- Additive, manual migration. Apply with all publishers stopped; keep the
-- feature disabled until schema verification and UTC-day initialization.
-- Baseline initialization for a partial day is an operator-verified step,
-- never inferred from application publication-completion flags.

CREATE TABLE IF NOT EXISTS publication_capacity_days (
    `utc_date` DATE NOT NULL,
    hard_cap INT UNSIGNED NOT NULL,
    baseline_count INT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (`utc_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS publication_capacity_reservations (
    rich_article_id INT NOT NULL,
    publication_key VARCHAR(191) NOT NULL,
    `utc_date` DATE NOT NULL,
    publication_at DATETIME NULL,
    create_started_at DATETIME NULL,
    wp_post_id BIGINT UNSIGNED NULL,
    PRIMARY KEY (rich_article_id),
    UNIQUE KEY uq_publication_capacity_key (publication_key),
    KEY idx_publication_capacity_date (`utc_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
