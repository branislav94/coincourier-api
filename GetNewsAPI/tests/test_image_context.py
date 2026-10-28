"""Offline subject-context fixtures; search and scoring thresholds stay unchanged."""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from image_search.context import context_rejection_reason
from image_search.emergency import rescue_images
from image_search.scoring import score_candidate
from image_search.selection import search_images
from tests.test_emergency_stock_rescue import StaticDownloader, StaticProvider, candidate as safe_candidate, image
from tests.test_wikimedia_provider import candidate as wikimedia_candidate


ARTICLE = {
    "title": "JPMorgan Brings Tokenized Money Market Funds to Solana",
    "seo_focus": "Solana tokenized money market funds", "category": "Markets",
    "hashtags": "solana,tokenization", "tickers": "SOL",
}
BAD_SUBJECT = "File:Symphony Brass Quartet - Solana Beach.jpg musician trumpet brass California San Diego beach"


def candidate(subject, *, provider="openverse", asset_id="context", score=.99, query="solana blockchain", **metadata):
    item = wikimedia_candidate() if provider == "wikimedia" else safe_candidate(provider, asset_id, score=score)
    item.final_score = score
    item.query = query
    item.metadata.update(metadata_text=subject, title=subject)
    item.metadata.update(metadata)
    return item


def article(project):
    return {"title": f"{project} blockchain development", "seo_focus": f"{project} crypto", "category": "Crypto"}


class ImageContextPolicyTests(unittest.TestCase):
    def test_exact_live_wrong_subject_scored_point_7225_but_is_hard_context_mismatch(self):
        item = candidate(BAD_SUBJECT, provider="wikimedia")
        score_candidate(item, ARTICLE)
        self.assertEqual(item.relevance_score, .65)
        self.assertEqual(item.topic_score, .95)
        self.assertEqual(item.final_score, .7225)
        self.assertGreaterEqual(item.final_score, item.provider_threshold)
        self.assertEqual(context_rejection_reason(item, ARTICLE), "context_mismatch")

    def test_legitimate_solana_subjects_keep_independent_crypto_or_brand_evidence(self):
        for subject in ("Solana logo", "Solana blockchain", "Solana cryptocurrency", "SOL token", "Crypto exchange screen tagged Solana SOL", "Solana validator wallet", "Solana digital assets"):
            with self.subTest(subject=subject):
                self.assertEqual(context_rejection_reason(candidate(subject), ARTICLE), "")

    def test_sparse_solana_name_without_corroboration_is_rejected(self):
        for subject in ("Solana", "Solana SOL", "Solana new update program event people"):
            with self.subTest(subject=subject):
                self.assertEqual(context_rejection_reason(candidate(subject), ARTICLE), "context_mismatch")

    def test_meaningful_second_article_concept_can_corroborate_the_subject(self):
        item = candidate("JPMorgan Solana headquarters")
        self.assertEqual(context_rejection_reason(item, ARTICLE), "")

    def test_shared_year_and_short_generic_words_are_not_meaningful_second_concepts(self):
        item = candidate("Solana Beach concert up 2026", query="solana up 2026")
        self.assertEqual(context_rejection_reason(item, {"title": "Solana crypto up in 2026"}), "context_mismatch")

    def test_article_source_alone_is_not_a_second_concept_but_title_match_is_preserved(self):
        item = candidate("JPMorgan Solana headquarters")
        self.assertEqual(context_rejection_reason(item, {**ARTICLE, "source_name": "JPMorgan"}), "")
        self.assertEqual(context_rejection_reason(item, {"title": "Solana blockchain news", "source_name": "JPMorgan"}), "context_mismatch")

    def test_polygon_geometry_rejects_and_matic_project_passes(self):
        for subject, expected in (
            ("Polygon triangle geometry vertices and angles", "context_mismatch"),
            ("Polygon geometry logo market exchange", "context_mismatch"),
            ("Polygon MATIC blockchain", ""), ("MATIC logo", ""),
        ):
            with self.subTest(subject=subject):
                self.assertEqual(context_rejection_reason(candidate(subject, query="polygon blockchain"), article("Polygon")), expected)

    def test_avalanche_snow_rejects_and_crypto_network_passes(self):
        for subject, expected in (
            ("Avalanche snow mountain ski glacier", "context_mismatch"),
            ("Avalanche network of ski trails logo", "context_mismatch"),
            ("Avalanche blockchain logo", ""), ("AVAX network", ""),
        ):
            with self.subTest(subject=subject):
                self.assertEqual(context_rejection_reason(candidate(subject, query="avalanche crypto"), article("Avalanche")), expected)

    def test_other_project_aliases_share_the_same_context_rule(self):
        for project, subject in (
            ("Near", "NEAR Protocol"), ("Stacks", "STX logo"), ("Optimism", "OP token wallet"),
            ("Arbitrum", "ARB exchange network"), ("Injective", "INJ crypto"),
            ("Cosmos", "Cosmos blockchain"), ("Stellar", "XLM logo"), ("Flow", "Flow blockchain"),
        ):
            with self.subTest(project=project):
                self.assertEqual(context_rejection_reason(candidate(subject, query=f"{project} blockchain"), article(project)), "")

    def test_other_unrelated_project_word_senses_are_rejected(self):
        for project, subject in (
            ("Stacks", "stacks of books in library"), ("Optimism", "optimism happiness smiling people"),
            ("Injective", "injective mathematical function bijection"), ("Cosmos", "cosmos flowers in garden"),
        ):
            with self.subTest(project=project):
                self.assertEqual(context_rejection_reason(candidate(subject, query=f"{project} blockchain"), article(project)), "context_mismatch")

    def test_requested_crypto_query_does_not_become_candidate_evidence(self):
        item = candidate(BAD_SUBJECT, query="solana blockchain crypto cryptocurrency token")
        self.assertEqual(context_rejection_reason(item, ARTICLE), "context_mismatch")

    def test_raw_attribution_does_not_become_relevance_text(self):
        item = candidate(BAD_SUBJECT)
        item.attribution_text += " <b>Solana blockchain cryptocurrency logo JPMorgan</b>"
        self.assertEqual(context_rejection_reason(item, ARTICLE), "context_mismatch")

    def test_creator_and_source_provenance_cannot_corroborate_wrong_subject(self):
        for field, value in (("creator", "Crypto Photographer"), ("source", "blockchain"), ("provider", "cryptocurrency")):
            with self.subTest(field=field):
                item = candidate(BAD_SUBJECT)
                item.metadata["metadata_text"] += f" {value}"
                if field == "creator":
                    item.creator_name = value
                else:
                    item.metadata[field] = value
                self.assertEqual(context_rejection_reason(item, ARTICLE), "context_mismatch")

    def test_creator_name_with_geographic_word_does_not_veto_legitimate_logo(self):
        item = candidate("Solana logo")
        item.creator_name = "Diego"
        item.metadata["metadata_text"] += " Diego"
        self.assertEqual(context_rejection_reason(item, ARTICLE), "")

    def test_aggregated_subject_keeps_project_name_when_creator_has_same_name(self):
        item = candidate("")
        item.metadata.pop("title")
        item.creator_name = "Solana"
        item.metadata["metadata_text"] = "Solana Beach brass quartet Solana"
        self.assertEqual(context_rejection_reason(item, ARTICLE), "context_mismatch")

    def test_url_host_path_query_and_html_link_targets_are_not_subject_evidence(self):
        item = candidate(BAD_SUBJECT + " https://crypto.example/blockchain/JPMorgan?q=cryptocurrency")
        item.source_page_url = "https://source.example/blockchain?crypto=Solana"
        item.metadata["description"] = '<a href="https://blockchain.example/crypto">Solana Beach brass quartet</a><script>cryptocurrency JPMorgan</script><style>blockchain</style>'
        self.assertEqual(context_rejection_reason(item, ARTICLE), "context_mismatch")

    def test_generic_network_market_logo_and_money_do_not_launder_explicit_wrong_sense(self):
        item = candidate("Solana Beach brass concert community network market logo token money funds")
        self.assertEqual(context_rejection_reason(item, ARTICLE), "context_mismatch")

    def test_richer_subject_metadata_overrides_a_misleading_filename(self):
        item = candidate("File:Solana Beach.jpg", description="Official Solana blockchain validator project logo")
        self.assertEqual(context_rejection_reason(item, ARTICLE), "")
        item.metadata["description"] = "Solana digital assets platform"
        self.assertEqual(context_rejection_reason(item, ARTICLE), "")
        item.metadata.update(title="Solana logo", metadata_text="Solana logo")
        item.source_page_url = "https://source.example/Solana_Beach_Diego.jpg"
        self.assertEqual(context_rejection_reason(item, ARTICLE), "")

    def test_structured_tags_and_source_page_title_are_normalized_subjects(self):
        item = candidate("File:Solana.jpg", tags=[{"name": "blockchain"}, {"name": "Solana"}])
        self.assertEqual(context_rejection_reason(item, ARTICLE), "")
        item.metadata.pop("tags")
        item.metadata["source_page_title"] = "Solana cryptocurrency logo"
        self.assertEqual(context_rejection_reason(item, ARTICLE), "")

    def test_filename_only_collision_cannot_supply_its_own_crypto_corroboration(self):
        item = candidate("")
        item.source_page_url = "https://source.example/Solana_Beach_blockchain_network_logo.jpg"
        self.assertEqual(context_rejection_reason(item, ARTICLE), "context_mismatch")

    def test_ordinary_non_crypto_article_senses_are_not_blanket_project_rejections(self):
        for title, subject, query in (
            ("Brass quartet performs in Solana Beach", "Solana Beach brass quartet", "solana beach music"),
            ("People near a forest feel optimism", "people near a forest show optimism", "forest people"),
            ("Polygon triangle geometry tutorial", "polygon geometry triangle", "polygon geometry"),
        ):
            with self.subTest(title=title):
                self.assertEqual(context_rejection_reason(candidate(subject, query=query), {"title": title}), "")

    def test_word_boundaries_do_not_match_project_name_substrings(self):
        self.assertEqual(context_rejection_reason(candidate("solar panels nearby optimistic people"), ARTICLE), "")

    def test_missing_url_field_is_left_to_required_field_safety(self):
        item = candidate("Solana blockchain")
        item.source_page_url = None
        self.assertEqual(context_rejection_reason(item, ARTICLE), "")


class ImageContextSelectionTests(unittest.TestCase):
    def search(self, item, *, article_context=None, local=(), budget=50, loader=None):
        return search_images(
            ARTICLE if article_context is None else article_context,
            providers=[StaticProvider(item.provider, [item])], downloader=loader or StaticDownloader(),
            local_usage=list(local), recent_wp_usage=[], global_candidate_limit=budget,
        )

    def test_mismatch_has_explicit_provider_reason_before_threshold_and_no_download(self):
        item = candidate(BAD_SUBJECT, provider="wikimedia")
        loader = StaticDownloader()
        with self.assertLogs("image_search.selection", level="INFO") as captured:
            strict = self.search(item, loader=loader)
        self.assertIsNone(strict.candidate)
        self.assertEqual(strict.rejection_counts, {"context_mismatch": 1})
        self.assertEqual(strict.provider_stats["wikimedia"]["rejected_by_reason"], {"context_mismatch": 1})
        self.assertEqual(strict.rescue_context.entries, [])
        self.assertEqual(loader.calls, [])
        self.assertIn("context_mismatch", " ".join(captured.output))

    def test_mismatch_is_hard_for_both_above_and_below_threshold_and_recent_candidates(self):
        for score, recent in ((.99, False), (.2, False), (.99, True), (.2, True)):
            with self.subTest(score=score, recent=recent), patch("image_search.selection.score_candidate", side_effect=lambda item, _article: item):
                item = candidate(BAD_SUBJECT, score=score)
                strict = self.search(item, local=[{"asset_key": item.asset_key}] if recent else [], budget=0)
                self.assertEqual(strict.rejection_counts, {"context_mismatch": 1})
                self.assertEqual(strict.rescue_context.entries, [])
                self.assertIsNone(rescue_images(strict).candidate)

    def test_rescue_independently_rechecks_context_in_every_tier(self):
        for tier in range(5):
            with self.subTest(tier=tier), patch("image_search.selection.score_candidate", side_effect=lambda item, _article: item):
                item = candidate("Solana blockchain", score=.6 if tier in {1, 3} else .99)
                content = image(b"valid observed original")
                loader = StaticDownloader({item.usable_url: content})
                local = [{"asset_key": item.asset_key}] if tier in {2, 3} else [{"content_sha256": content.content_sha256}] if tier == 4 else []
                strict = self.search(item, local=local, budget=0 if tier == 0 else 50, loader=loader)
                self.assertIsNone(strict.candidate)
                self.assertEqual(len(strict.rescue_context.entries), 1)
                item.metadata.update(title=BAD_SUBJECT, metadata_text=BAD_SUBJECT)
                validator = Mock(return_value=True)
                before = len(loader.calls)
                result = rescue_images(strict, post_download_validator=validator)
                self.assertIsNone(result.candidate)
                self.assertEqual(strict.rescue_context.entries[0].hard_reason, "context_mismatch")
                self.assertEqual(len(loader.calls), before)
                validator.assert_not_called()

    def test_rescue_retains_only_original_image_article_context_not_body(self):
        source_article = {**ARTICLE, "body": "private article body"}
        with patch("image_search.selection.score_candidate", side_effect=lambda item, _article: item):
            item = candidate("Solana blockchain")
            strict = self.search(item, article_context=source_article, budget=0)
        source_article["title"] = "Bitcoin market update"
        self.assertEqual(strict.rescue_context.article_context["title"], ARTICLE["title"])
        self.assertNotIn("body", strict.rescue_context.article_context)
        item.metadata.update(title=BAD_SUBJECT, metadata_text=BAD_SUBJECT)
        self.assertIsNone(rescue_images(strict).candidate)

    def test_existing_unambiguous_provider_selections_remain_unchanged(self):
        bitcoin_article = {"title": "Bitcoin cryptocurrency markets", "seo_focus": "Bitcoin cryptocurrency", "category": "Markets"}
        for provider in ("pexels", "pixabay", "wikimedia"):
            with self.subTest(provider=provider):
                item = candidate("Bitcoin cryptocurrency blockchain markets", provider=provider, query="bitcoin cryptocurrency")
                strict = self.search(item, article_context=bitcoin_article)
                self.assertIs(strict.candidate, item)
                self.assertNotIn("context_mismatch", strict.rejection_counts)


if __name__ == "__main__":
    unittest.main()
