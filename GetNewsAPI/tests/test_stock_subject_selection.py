'''Offline stock subject regressions reconstructed from the editorial audit.'''
from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
from typing import Any
import unittest
from unittest.mock import patch

from image_search import emergency, selection
from image_search.models import DownloadedImage, ImageCandidate, ImageSearchResult
from image_search.provider import ImageSearchProvider
from image_search.providers.pexels import PexelsProvider
from image_search.providers.pixabay import PixabayProvider
from publishing.wordpress import publisher


@dataclass(frozen=True)
class AuditExample:
    '''The WordPress ID identifies audit evidence, not a historical search run.'''

    wordpress_id: int
    headline: str
    lead: str
    tags: str
    alt: str

    def article(self) -> dict[str, Any]:
        return {'id': self.wordpress_id, 'title': self.headline, 'full_text': self.lead}


MISMATCHES = (
    AuditExample(8606, 'Chainflip responds to Ethereum vault withdrawal',
                 'The Chainflip protocol investigated a withdrawal from its Ethereum vault.',
                 'monastery, vault, cloister, medieval architecture', 'Stone vaults over a monastery cloister'),
    AuditExample(8591, 'CYBER investigates cross-chain bridge incident',
                 'The CYBER token team investigated an incident involving its blockchain bridge.',
                 'bridge, park, garden, Japan', 'A wooden footbridge in a Japanese garden'),
    AuditExample(8579, 'BlackRock Bitcoin ETF records largest outflow',
                 'BlackRock’s Bitcoin ETF reported a record outflow.',
                 'record player, vinyl, music', 'Vinyl on a record player'),
    AuditExample(8573, 'Bitcoin falls below $56,000',
                 'Bitcoin traded below $56,000 as the cryptocurrency market declined.',
                 'bullets, magazine, 56 mm, ammunition', '56 mm bullets and a magazine'),
    AuditExample(8564, 'Bitcoin hits a low amid $2.4B in crypto liquidations',
                 'Bitcoin and the wider crypto market fell during a wave of liquidations.',
                 'low-key portrait, man, hand, dark background', 'Low-key portrait of a man reaching toward the camera'),
    AuditExample(8540, 'Bitcoin drops below $56K as ETF outflows and legislation draw attention',
                 'Bitcoin fell while traders watched ETF flows and pending legislation.',
                 'analysis, biochemistry, microscope', 'Microscope in a biochemistry laboratory'),
    AuditExample(8537, 'Bitcoin drops below its summer low',
                 'The Bitcoin market fell below its previous summer low.',
                 'cherries, waterdrop, fruit', 'Drops of water on ripe cherries'),
    AuditExample(8588, 'Unexpected minting of CYBER on Solana',
                 'The CYBER token was unexpectedly minted on Solana.',
                 'Dash coin, crypto, token', 'A coin marked with the Dash cryptocurrency logo'),
    AuditExample(8615, 'XRP long liquidations rise',
                 'Liquidations hit traders holding XRP long positions.',
                 'Bitcoin/Tether, trading screen, chart', 'Bitcoin/Tether trading screen with a price chart'),
    AuditExample(8659, 'XRP Ledger activity declines',
                 'Activity across the XRP Ledger fell.',
                 'Bitcoin, upward arrow, cryptocurrency', 'Bitcoin with an upward arrow'),
)


def pixabay_hit(asset_id: int, tags: str) -> dict[str, Any]:
    '''Supply the fields of a Pixabay photo hit without a network request.'''
    return {
        'id': asset_id, 'tags': tags, 'pageURL': f'https://pixabay.com/photos/image-{asset_id}/',
        'largeImageURL': f'https://cdn.pixabay.com/photo/2026/01/01/00/00/image-{asset_id}.jpg',
        'user': 'Fixture Contributor', 'user_id': 41, 'imageWidth': 1600, 'imageHeight': 900,
        'views': 1000, 'downloads': 100, 'likes': 5,
    }


def pexels_photo(asset_id: int, alt: str) -> dict[str, Any]:
    '''Supply a Pexels photo with its usual generic image filename.'''
    return {
        'id': asset_id, 'alt': alt, 'url': f'https://www.pexels.com/photo/image-{asset_id}/',
        'src': {'large2x': f'https://images.pexels.com/photos/{asset_id}/pexels-photo-{asset_id}.jpeg'},
        'photographer': 'Fixture Contributor', 'width': 1600, 'height': 900,
    }


class FixtureResponses:
    '''Return offline provider JSON and record the production queries it received.'''

    def __init__(
        self, field: str, items: Sequence[dict[str, Any]], *, next_items: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.field = field
        self.pages = (list(items), list(next_items))
        self.calls: list[tuple[str, str]] = []

    def get_json(self, provider: str, query: str, url: str, **kwargs: Any) -> dict[str, Any]:
        index = len(self.calls)
        self.calls.append((provider, query))
        payload = {self.field: self.pages[index] if index < len(self.pages) else []}
        if not kwargs['payload_validator'](payload):
            raise ValueError(f'Invalid fixture response for {url}')
        return payload


class FixtureDownloads:
    '''Return valid download metadata and record exactly which assets were tried.'''

    def __init__(self) -> None:
        self.calls: list[str] = []

    def download(self, candidate: ImageCandidate) -> DownloadedImage:
        self.calls.append(candidate.asset_key)
        content = candidate.asset_key.encode()
        digest = hashlib.sha256(content).hexdigest()
        return DownloadedImage(content, 'image/jpeg', 1600, 900, digest, digest[:16])


def providers_for(
    *, photos: Sequence[dict[str, Any]] = (), hits: Sequence[dict[str, Any]] = (),
) -> tuple[list[ImageSearchProvider], list[FixtureResponses]]:
    '''Exercise real adapters with zero thresholds to isolate hard eligibility.'''
    providers: list[ImageSearchProvider] = []
    clients = []
    if photos:
        client = FixtureResponses('photos', photos)
        providers.append(PexelsProvider(client=client, api_key='fixture', enabled=True, threshold=0.0))
        clients.append(client)
    if hits:
        client = FixtureResponses('hits', hits)
        providers.append(PixabayProvider(client=client, api_key='fixture', enabled=True, threshold=0.0))
        clients.append(client)
    return providers, clients


@contextmanager
def subject_mode(mode: str) -> Iterator[None]:
    '''Set the decision mode at both production selector boundaries.'''
    with (
        patch.object(selection, 'STOCK_IMAGE_SUBJECT_MODE', mode),
        patch.object(emergency, 'STOCK_IMAGE_SUBJECT_MODE', mode),
    ):
        yield


def select(
    article: dict[str, Any], providers: list[ImageSearchProvider], downloads: FixtureDownloads,
    *, limit: int = 20, **kwargs: Any,
) -> ImageSearchResult:
    '''Run V2 against mock transports and empty usage history.'''
    return selection.search_images(
        article, providers=providers, downloader=downloads, local_usage=[], recent_wp_usage=[],
        global_candidate_limit=limit, **kwargs,
    )


class StockSubjectSelectionTests(unittest.TestCase):
    def test_audit_mismatches_are_hard_rejections_in_both_routes(self) -> None:
        for example in MISMATCHES:
            asset_id = example.wordpress_id + 10000
            for provider_name, fixture in (
                ('pixabay', {'hits': [pixabay_hit(asset_id, example.tags)]}),
                ('pexels', {'photos': [pexels_photo(asset_id, example.alt)]}),
            ):
                with self.subTest(post=example.wordpress_id, provider=provider_name):
                    article = example.article()
                    providers, _ = providers_for(**fixture)
                    downloads = FixtureDownloads()
                    with subject_mode('enforce'), self.assertLogs(level='INFO') as logs:
                        strict = select(article, providers, downloads)
                        rescued = emergency.rescue_images(strict, enabled=True)
                    self.assertIsNone(strict.candidate)
                    self.assertIsNone(rescued.candidate)
                    self.assertEqual(downloads.calls, [])
                    self.assertEqual(strict.rejection_counts, {'context_mismatch': 1})
                    self.assertEqual(strict.provider_stats[provider_name]['normalized'], 1)
                    self.assertEqual(strict.rescue_context.entries, [])
                    self.assertTrue(rescued.emergency_summary['emergency_completed'])
                    reason = 'asset_mismatch' if example.wordpress_id in {8588, 8615, 8659} else 'unrelated_subject'
                    for route in ('strict', 'rescue'):
                        self.assertTrue(any(
                            '[IMG-SUBJECT]' in line and f'article={strict.article_correlation_id}' in line
                            and f'provider={provider_name} asset_id={asset_id} route={route}' in line
                            and f'reason={reason}' in line
                            for line in logs.output
                        ), logs.output)

                    providers, _ = providers_for(**fixture)
                    with subject_mode('off'):
                        baseline = select(article, providers, downloads)
                    self.assertEqual(baseline.candidate.asset_key, f'{provider_name}:{asset_id}')

    def test_rescue_rechecks_a_mismatch_in_the_observed_pool(self) -> None:
        example = MISMATCHES[-2]
        article = {**example.article(), 'title': 'Long liquidations rise after a quiet week'}
        providers, _ = providers_for(photos=[pexels_photo(20201, example.alt)])
        downloads = FixtureDownloads()
        with subject_mode('shadow'):
            strict = select(article, providers, downloads, limit=0)
        self.assertIsNone(strict.candidate)
        self.assertEqual(len(strict.rescue_context.entries), 1)
        with subject_mode('enforce'):
            rescued = emergency.rescue_images(strict, enabled=True)
        self.assertIsNone(rescued.candidate)
        self.assertEqual(strict.rescue_context.entries[0].hard_reason, 'context_mismatch')
        self.assertEqual(downloads.calls, [])

    def test_sparse_duplicate_cannot_reintroduce_an_identified_mismatch(self) -> None:
        example = MISMATCHES[-1]
        asset_id = 20205
        sparse = pexels_photo(asset_id, '')
        sparse['src']['large2x'] += '?auto=compress'
        for limit in (0, 20):
            with self.subTest(limit=limit), subject_mode('enforce'):
                client = FixtureResponses('photos', [sparse], next_items=[pexels_photo(asset_id, example.alt)])
                provider = PexelsProvider(client=client, api_key='fixture', enabled=True, threshold=0.0)
                downloads = FixtureDownloads()
                strict = select(example.article(), [provider], downloads, limit=limit)
                rescued = emergency.rescue_images(strict, enabled=True)
                self.assertEqual(strict.provider_stats['pexels']['normalized'], 2)
                self.assertIsNone(strict.candidate)
                self.assertIsNone(rescued.candidate)
                self.assertEqual(downloads.calls, [])

    def test_audit_positives_and_neutral_metadata_stay_eligible(self) -> None:
        positives = (
            (8597, 'XRP price climbs after a volatile week', 'Silver XRP cryptocurrency coin'),
            (8483, 'Hong Kong reviews rules for digital asset exchanges', 'Hong Kong skyline over Victoria Harbour'),
            (8582, 'ECB announces progress on the digital euro', 'European Central Bank headquarters in Frankfurt'),
            (8456, 'Bitcoin price rises as traders turn bullish', 'Bitcoin trading chart on a screen'),
            (21001, 'XRP Ledger activity declines', ''),
            (21002, 'XRP Ledger activity declines', 'A trading screen with a candlestick chart'),
            (21003, 'Bitcoin outperforms XRP during volatile trading', 'An XRP coin on a price chart'),
            (21004, 'XRP outperforms Bitcoin during volatile trading', 'Bitcoin chart with an upward arrow'),
            (21005, 'Japan reopens a garden footbridge funded by crypto donations', 'A wooden footbridge in a Japanese garden'),
            (21006, 'Jerome Powell speaks on Bitcoin and interest rates', 'Low-key portrait of Federal Reserve chair Jerome Powell'),
            (21007, 'Bitcoin ETF outflows continue', 'Portrait of BlackRock CEO Larry Fink discussing Bitcoin ETFs'),
            (21009, 'XRP Ledger activity declines', 'Trading desk near coin display'),
        )
        for article_id, title, alt in positives:
            with self.subTest(post=article_id), subject_mode('enforce'):
                article = {'id': article_id, 'title': title}
                asset_id = article_id + 20000
                photo = pexels_photo(asset_id, alt)
                providers, _ = providers_for(photos=[photo])
                downloads = FixtureDownloads()
                result = select(article, providers, downloads)
                self.assertIsNotNone(result.candidate)
                self.assertEqual(downloads.calls, [f'pexels:{asset_id}'])

    def test_incidental_bitcoin_context_does_not_turn_xrp_story_into_comparison(self) -> None:
        headline = 'XRP long liquidations rise amid broader Bitcoin volatility'
        photo = pexels_photo(20207, 'Bitcoin trading screen with a price chart')
        for lead, wanted in (
            ('XRP traders saw more positions liquidated during the session.', []),
            ('XRP underperformed Bitcoin during the session.', ['pexels:20207']),
        ):
            with self.subTest(lead=lead), subject_mode('enforce'):
                providers, _ = providers_for(photos=[photo])
                downloads = FixtureDownloads()
                result = select({'id': 21010, 'title': headline, 'full_text': lead}, providers, downloads)
                self.assertEqual(downloads.calls, wanted)
                self.assertEqual(result.candidate is not None, bool(wanted))

    def test_provenance_cannot_requalify_a_mismatched_subject(self) -> None:
        example = MISMATCHES[0]
        photo = pexels_photo(20101, example.alt)
        photo.update(
            photographer='Chainflip Ethereum Blockchain',
            photographer_url='https://www.pexels.com/@ethereum-vault-crypto/',
            url='https://www.pexels.com/photo/chainflip-ethereum-blockchain-vault-20101/',
        )
        downloads = FixtureDownloads()
        providers, clients = providers_for(photos=[photo])
        with subject_mode('enforce'):
            result = select({**example.article(), 'seo_focus': 'Ethereum blockchain security'}, providers, downloads)
        self.assertTrue(any('ethereum' in query.lower() for _, query in clients[0].calls))
        self.assertIsNone(result.candidate)
        self.assertEqual(downloads.calls, [])

    def test_enforce_continues_to_suitable_candidate_in_the_same_response(self) -> None:
        example = MISMATCHES[-1]
        wrong = pexels_photo(20201, example.alt)
        wrong['url'] = 'https://www.pexels.com/photo/xrp-ledger-activity-declines-20201/'
        photos = [wrong, pexels_photo(20202, 'XRP cryptocurrency coin')]
        providers, _ = providers_for(photos=photos)
        downloads = FixtureDownloads()
        with subject_mode('off'):
            baseline = select(example.article(), providers, downloads)
        self.assertEqual(baseline.candidate.asset_key, 'pexels:20201')

        providers, _ = providers_for(photos=photos)
        downloads = FixtureDownloads()
        with subject_mode('enforce'):
            result = select(example.article(), providers, downloads)
        self.assertEqual(result.candidate.asset_key, 'pexels:20202')
        self.assertEqual(result.rejection_counts, {'context_mismatch': 1})
        self.assertEqual(downloads.calls, ['pexels:20202'])

    def test_shadow_preserves_stock_choice_calls_and_fallback_in_both_routes(self) -> None:
        example = MISMATCHES[-1]
        for limit in (0, 20):
            snapshots = []
            for mode in ('off', 'shadow'):
                providers, clients = providers_for(
                    photos=[pexels_photo(20203, example.alt)],
                    hits=[pixabay_hit(20204, 'xrp, cryptocurrency, coin')] if limit else [],
                )
                downloads = FixtureDownloads()
                with subject_mode(mode), self.assertLogs(level='INFO') as logs:
                    strict = select(example.article(), providers, downloads, limit=limit)
                    result = emergency.rescue_images(strict, enabled=True)
                self.assertEqual(result.candidate.asset_key, 'pexels:20203')
                snapshots.append((
                    strict.rejection_counts, strict.provider_stats, strict.generation_allowed,
                    strict.all_available_providers_exhausted, strict.search_budget_exhausted,
                    result.emergency_summary, [client.calls for client in clients], downloads.calls,
                ))
                if mode == 'shadow':
                    for route in ('strict', 'rescue'):
                        self.assertIn(f'route={route} mode=shadow decision=would_reject', ' '.join(logs.output))
            self.assertEqual(snapshots[0], snapshots[1])

    def test_existing_ambiguous_project_guard_stays_active_in_every_mode(self) -> None:
        article = {'id': 21008, 'title': 'Solana blockchain validator software updated'}
        for mode in ('off', 'shadow', 'enforce'):
            with self.subTest(mode=mode), subject_mode(mode):
                providers, _ = providers_for(photos=[pexels_photo(20206, 'Solana Beach brass quartet')])
                downloads = FixtureDownloads()
                strict = select(article, providers, downloads)
                rescued = emergency.rescue_images(strict, enabled=True)
                self.assertEqual(strict.rejection_counts, {'context_mismatch': 1})
                self.assertIsNone(strict.candidate)
                self.assertIsNone(rescued.candidate)
                self.assertEqual(downloads.calls, [])

    def test_all_mismatches_use_only_configured_generation_fallback(self) -> None:
        example = MISMATCHES[-1]
        article = example.article()
        for generation_enabled in (False, True):
            photos = [pexels_photo(20301, example.alt), pexels_photo(20302, 'Bitcoin trading screen')]
            providers, _ = providers_for(photos=photos)
            downloads = FixtureDownloads()

            def strict(article_context: dict[str, Any], **kwargs: Any) -> ImageSearchResult:
                return select(article_context, providers, downloads, **kwargs)

            with self.subTest(generation_enabled=generation_enabled), subject_mode('enforce'), ExitStack() as stack:
                for name, value in {
                    'IMAGE_SEARCH_ENGINE': 'v2', 'IMAGE_SOURCE_MODE': 'stock', 'IMAGE_SOURCE_PRIORITY': 'stock_first',
                    'USE_SOURCE_IMAGES': False, 'IMAGE_REQUIRE_FEATURED_IMAGE': False,
                    'IMAGE_EMERGENCY_STOCK_RESCUE_ENABLED': True, 'IMAGE_GENERATION_ONLY_AFTER_SEARCH_EXHAUSTED': True,
                    'IMAGE_GENERATION_ON_PROVIDER_ERROR': False, 'IMAGE_FALLBACK_PROVIDER': 'openai',
                    'OPENAI_IMAGE_FALLBACK': generation_enabled,
                }.items():
                    stack.enter_context(patch.object(publisher, name, value))
                search = stack.enter_context(patch.object(publisher, 'search_images', side_effect=strict))
                generate = stack.enter_context(patch.object(publisher, '_generated_image_content', return_value=(
                    b'fixture generated image', 'generated.jpg', {'provider': 'openai'},
                )))
                upload = stack.enter_context(patch.object(publisher, 'upload_media', return_value=2001))
                stack.enter_context(patch.object(publisher, 'set_media_details', return_value=True))
                media_id, metadata = publisher.upload_image(None, article['title'], '', article=article)
            search.assert_called_once()
            self.assertEqual(downloads.calls, [])
            self.assertFalse(metadata['strict_stock_selected'])
            self.assertTrue(metadata['emergency_stock_rescue_attempted'])
            self.assertTrue(metadata['emergency_completed'])
            self.assertFalse(metadata['emergency_selected'])
            self.assertEqual(metadata['emergency_candidates_available'], 0)
            if generation_enabled:
                self.assertEqual(media_id, 2001)
                self.assertEqual(metadata['final_image_source'], 'openai')
                self.assertFalse(metadata['final_image_failure'])
                generate.assert_called_once_with('openai', article['title'], '', fallback_used=True)
                upload.assert_called_once()
            else:
                self.assertIsNone(media_id)
                self.assertTrue(metadata['final_image_failure'])
                generate.assert_not_called()
                upload.assert_not_called()
