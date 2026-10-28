"""Original-source fixtures for conservative cross-source event confirmation."""
from __future__ import annotations

import unittest

from duplicate_detection.policy import AssessmentType, assess_relationship
from duplicate_detection.event_matching import extract_event_facts
from duplicate_detection.enforcement import _confirmed as confirmed_duplicate


def fomc_source_pair(*, event_ids=("fed-wire-a", "fed-wire-b")):
    """Return (prior raw 18789, current raw 18825); IDs are fixture data only."""
    prior = {
        "id": 18789, "news_id": "source-fed-18789", "event_id": event_ids[0],
        "news_url": "https://first-source.example/fed-decision",
        "title": "Fed holds federal funds rate",
        "full_text": "The Federal Reserve held its federal funds target range at 3.75%-4.00% on October 28, 2026. The FOMC voted 10-2. Bitcoin traded near $81,000.",
        "source_name": "First Source", "publish_date": "2026-10-28 18:01:00", "tickers": "BTC",
    }
    current = {
        "id": 18825, "news_id": "source-fed-18825", "event_id": event_ids[1],
        "news_url": "https://second-source.example/fomc-crypto",
        "title": "FOMC meeting impact on crypto markets: Fed holds rates at 3.75%-4.00%",
        "full_text": "At the October 28, 2026 FOMC meeting, the Federal Reserve maintained the federal funds rate range of 3.75 to 4.00%. A 10 to 2 vote kept that target unchanged. Bitcoin was near $81,000.",
        "source_name": "Second Source", "publish_date": "2026-10-28 18:05:00", "tickers": "BTC",
    }
    return prior, current


def source_pair(prior_title, current_title, prior_body, current_body):
    prior, current = fomc_source_pair()
    prior.update(title=prior_title, full_text=prior_body, tickers="")
    current.update(title=current_title, full_text=current_body, tickers="")
    return prior, current


class CrossSourcePolicyTests(unittest.TestCase):
    def assess(self, prior=None, current=None):
        if prior is None:
            prior, current = fomc_source_pair()
        return assess_relationship(current, prior)

    def assert_cross_source(self, prior, current):
        result = self.assess(prior, current)
        self.assertEqual(result.assessment_type, AssessmentType.SAME_EVENT_DUPLICATE)
        self.assertFalse(result.same_event_id)
        self.assertFalse(result.same_content_hash)
        self.assertEqual(result.event_identity_source, "structured_cross_source")
        self.assertGreaterEqual(len(result.confirmation_anchors), 2)
        self.assertEqual(result.new_fact_codes, ())
        self.assertEqual(result.contradiction_flags, ())
        self.assertEqual(result.evidence_uncertainty, ())
        return result

    def assert_not_cross_source_duplicate(self, prior, current):
        result = self.assess(prior, current)
        self.assertFalse(confirmed_duplicate(result))
        return result

    def test_fomc_real_dev_pair_different_ids_low_title_overlap_distinct_bodies(self):
        prior, current = fomc_source_pair()
        result = self.assert_cross_source(prior, current)
        self.assertEqual((prior["id"], current["id"]), (18789, 18825))
        self.assertLess(result.title_token_jaccard, 0.2)
        self.assertEqual(result.confirmation_anchors, ("rate_range:3.75%-4%", "vote:10-2"))

    def test_fomc_one_or_both_provider_event_ids_missing(self):
        for ids in ((None, "other"), ("prior", None), (None, None)):
            with self.subTest(ids=ids):
                self.assert_cross_source(*fomc_source_pair(event_ids=ids))

    def test_matching_provider_event_id_remains_strongest_identity_source(self):
        prior, current = fomc_source_pair(event_ids=("same-provider-event", "same-provider-event"))
        result = self.assess(prior, current)
        self.assertEqual(result.assessment_type, AssessmentType.SAME_EVENT_DUPLICATE)
        self.assertTrue(result.same_event_id)
        self.assertEqual(result.event_identity_source, "provider_event_id")

    def test_rate_and_vote_equivalent_formats_preserve_typed_roles(self):
        prior, current = fomc_source_pair()
        for rate, vote in (("3.75\u20134.0%", "10\u20132"), ("3.750% to 4%", "10-2")):
            with self.subTest(rate=rate):
                current["title"] = "Federal Reserve holds rates"
                current["full_text"] = f"On October 28, 2026 the Fed held its federal funds target at {rate}. The FOMC recorded a {vote} vote. Bitcoin traded near $81,000."
                self.assert_cross_source(prior, current)

    def test_all_four_non_fomc_supported_event_classes(self):
        pairs = [
            source_pair("JPMorgan Brings Tokenized Money Market Funds to Solana", "J.P. Morgan Adds Tokenized Money Market Funds on Solana", "JPMorgan launched tokenized money market funds on Solana on October 28, 2026.", "On October 28, 2026 J.P. Morgan introduced tokenized money market funds on Solana."),
            source_pair("Ethereum Postpones Glamsterdam Hoodi Testnet Activation", "Ethereum Foundation Delays Glamsterdam Hoodi Testnet Fork", "Ethereum Foundation postponed the Hoodi testnet activation for Glamsterdam on October 28, 2026.", "On October 28, 2026 Ethereum Foundation delayed the Glamsterdam fork on the Hoodi testnet."),
            source_pair("Solana Alpenglow Mainnet Activation", "Solana Activates Alpenglow on Mainnet", "Solana validators activated Alpenglow on mainnet on October 28, 2026 with 1000 transactions.", "Alpenglow mainnet activation on Solana took place on October 28, 2026 with 1000 transactions."),
            source_pair("Bitcoin Core Updates AI Security Policy", "Bitcoin Core Revises Security Policy for AI Bugs", "Bitcoin Core updated its security policy for AI-enabled vulnerability discovery on October 28, 2026.", "On October 28, 2026 Bitcoin Core revised the security policy for AI-discoverable bugs."),
        ]
        for prior, current in pairs:
            with self.subTest(title=current["title"]):
                self.assert_cross_source(prior, current)

    def test_jpmorgan_money_market_hyphen_or_en_dash_is_same_product(self):
        for separator in ("-", "\u2013"):
            with self.subTest(separator=separator):
                prior, current = source_pair("JPMorgan launches tokenized money market funds on Solana", "JPMorgan adds tokenized funds to Solana", "JPMorgan launched tokenized money market funds on Solana on October 28, 2026.", f"JPMorgan introduced tokenized money{separator}market funds on Solana on October 28, 2026.")
                self.assert_cross_source(prior, current)

    def test_entity_only_or_entity_action_without_strong_anchors_allows(self):
        for body in ("Federal Reserve and Bitcoin market discussion.", "The Federal Reserve held rates on October 28, 2026."):
            with self.subTest(body=body):
                prior, current = source_pair("Federal Reserve update", "Fed policy discussion", body, body + " More background.")
                self.assert_not_cross_source_duplicate(prior, current)

    def test_same_range_without_vote_or_meeting_is_only_one_anchor(self):
        prior, current = source_pair("Fed holds rates", "Federal Reserve holds policy rate", "The Federal Reserve held its federal funds range at 3.75%-4% on October 28, 2026.", "On October 28, 2026 the Fed maintained the federal funds range of 3.75 to 4.00%.")
        result = self.assert_not_cross_source_duplicate(prior, current)
        self.assertEqual(result.confirmation_anchors, ("rate_range:3.75%-4%",))

    def test_explicit_full_event_date_required_not_publication_time(self):
        prior, current = fomc_source_pair()
        for row in (prior, current):
            row["full_text"] = row["full_text"].replace("October 28, 2026", "October 28")
        self.assert_not_cross_source_duplicate(prior, current)

    def test_different_calendar_date_is_not_same_event_even_same_other_facts(self):
        prior, current = fomc_source_pair()
        current["full_text"] = current["full_text"].replace("October 28", "October 29")
        result = self.assert_not_cross_source_duplicate(prior, current)
        self.assertIn("different_event_date", result.contradiction_flags)

    def test_different_action_same_date_is_related_even_lexically_close(self):
        prior, current = fomc_source_pair()
        current["title"] = "Fed cuts federal funds rate"
        current["full_text"] = "The Federal Reserve cut its federal funds target range to 3.75%-4.00% on October 28, 2026. The FOMC voted 10-2. Bitcoin traded near $81,000."
        result = self.assert_not_cross_source_duplicate(prior, current)
        self.assertEqual(result.assessment_type, AssessmentType.RELATED_EVENT)
        self.assertIn("different_action_or_status", result.contradiction_flags)

    def test_new_range_or_vote_is_material_despite_only_one_remaining_anchor(self):
        for old, new in (("3.75", "3.50"), ("10 to 2", "9 to 3")):
            with self.subTest(new=new):
                prior, current = fomc_source_pair()
                current["title"] = current["title"].replace(old, new)
                current["full_text"] = current["full_text"].replace(old, new)
                result = self.assert_not_cross_source_duplicate(prior, current)
                self.assertEqual(result.assessment_type, AssessmentType.MATERIAL_UPDATE)
                self.assertIn("new_numeric_value", result.new_fact_codes)
                self.assertTrue(result.contradiction_flags)

    def test_later_guidance_forecast_new_actor_and_market_moves_allowed(self):
        additions = (
            "Powell press conference adds new guidance.", "The Fed issued a revised forecast.",
            "Governor Lisa Cook dissents from the committee consensus.", "Michelle Bowman opposed the decision.",
            "The Federal Reserve expects inflation to accelerate.", "Powell says monetary policy is now restrictive enough.",
            "Bitcoin tumbles as traders reassess the decision.", "Bitcoin retreats as traders reassess the decision.",
            "Bitcoin rises to $82,000 after the policy decision.",
        )
        for addition in additions:
            with self.subTest(addition=addition):
                prior, current = fomc_source_pair()
                current["full_text"] += " " + addition
                result = self.assert_not_cross_source_duplicate(prior, current)
                self.assertTrue(result.new_fact_codes or result.evidence_uncertainty)

    def test_multiple_full_dates_cannot_be_swapped_without_role_keywords(self):
        prior, current = fomc_source_pair()
        prior["full_text"] += " Another meeting occurs October 29, 2026."
        current["full_text"] = current["full_text"].replace("October 28", "October 29") + " Another meeting occurs October 28, 2026."
        result = self.assert_not_cross_source_duplicate(prior, current)
        self.assertIn("unresolved_multiple_event_dates", result.evidence_uncertainty)

    def test_two_named_funds_are_different_targets_not_category_equivalents(self):
        prior, current = source_pair("JPMorgan launches Alpha Treasury Fund on Solana", "JPMorgan introduces Beta Government Fund on Solana", "JPMorgan launched the Alpha Treasury Fund as tokenized money market funds on Solana on October 28, 2026.", "JPMorgan introduced the Beta Government Fund as tokenized money market funds on Solana on October 28, 2026.")
        result = self.assert_not_cross_source_duplicate(prior, current)
        self.assertIn("different_named_target", result.contradiction_flags)

    def test_testnet_activation_and_client_release_are_distinct_milestones(self):
        for same_ids in (True, False):
            with self.subTest(same_ids=same_ids):
                prior, current = source_pair("Ethereum postpones Hoodi testnet activation for Glamsterdam", "Ethereum delays Glamsterdam client release for Hoodi testnet", "Ethereum postponed the Hoodi testnet activation for Glamsterdam on October 28, 2026.", "Ethereum delayed the Glamsterdam client release for the Hoodi testnet on October 28, 2026.")
                if same_ids:
                    current["event_id"] = prior["event_id"]
                self.assert_not_cross_source_duplicate(prior, current)
                self.assert_not_cross_source_duplicate(current, prior)

    def test_postponement_with_new_explicit_activation_outcome_is_material(self):
        for same_ids in (True, False):
            with self.subTest(same_ids=same_ids):
                title = "Ethereum postpones Hoodi Glamsterdam testnet activation"
                body = "Ethereum Foundation postponed the Hoodi testnet activation for Glamsterdam on October 28, 2026."
                prior, current = source_pair(title, title, body, body + " The Hoodi testnet fork has now activated successfully.")
                if same_ids:
                    current["event_id"] = prior["event_id"]
                result = self.assert_not_cross_source_duplicate(prior, current)
                self.assertEqual(result.assessment_type, AssessmentType.MATERIAL_UPDATE)
                self.assertIn("new_action_or_status", result.new_fact_codes)

    def test_security_disclosure_rules_and_reward_rules_are_different_revisions(self):
        prior, current = source_pair("Bitcoin Core updates security policy", "Bitcoin Core revises security policy", "Bitcoin Core updated its security policy for AI vulnerability discovery disclosure rules on October 28, 2026.", "Bitcoin Core revised its security policy for AI vulnerability discovery reward rules on October 28, 2026.")
        result = self.assert_not_cross_source_duplicate(prior, current)
        self.assertIn("different_named_target", result.contradiction_flags)

    def test_same_protocol_other_milestone_does_not_confirm(self):
        prior, current = source_pair("Solana Alpenglow mainnet activation", "Solana Alpenglow testnet activation", "Solana activated Alpenglow on mainnet on October 28, 2026.", "Solana activated Alpenglow on testnet on October 28, 2026.")
        self.assert_not_cross_source_duplicate(prior, current)

    def test_source_provenance_and_generic_crypto_terms_are_not_strong_anchors(self):
        prior, current = source_pair("Solana blockchain network crypto", "Solana blockchain network crypto", "Solana blockchain network crypto on October 28, 2026.", "Solana blockchain network crypto on October 28, 2026. Different wording.")
        prior["source_name"] = current["source_name"] = "Federal Reserve FOMC"
        result = self.assert_not_cross_source_duplicate(prior, current)
        self.assertEqual(result.confirmation_anchors, ())

    def test_range_roles_remain_uncertain_when_prior_and_current_ranges_both_present(self):
        prior, current = fomc_source_pair()
        prior["full_text"] += " Previously the range was 3.50%-3.75%."
        current["full_text"] += " Previously the range was 3.50%-3.75%."
        result = self.assert_not_cross_source_duplicate(prior, current)
        self.assertIn("unresolved_numeric_roles", result.evidence_uncertainty)


if __name__ == "__main__":
    unittest.main()
