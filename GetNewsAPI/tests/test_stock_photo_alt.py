'''Verify photo-grounded alt text through the WordPress attachment boundary.'''

from __future__ import annotations

from contextlib import ExitStack
from functools import partial
import hashlib
from pathlib import Path
import sys
from typing import Any
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

from GetNewsAPI.tests.environment_isolation import install_test_environment

install_test_environment()
PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from image_search import emergency, registry, selection
import stock_images
from image_search.downloader import ImageDownloader
from image_search.models import DownloadedImage
from image_search.providers.pexels import PexelsProvider
from image_search.providers.pixabay import PixabayProvider
from image_search.providers.wikimedia import WikimediaProvider
from publishing.wordpress import publisher
from GetNewsAPI.tests.test_stock_image_vision import fixture_jpeg
from GetNewsAPI.tests.test_stock_subject_selection import FixtureResponses, pexels_photo, pixabay_hit
from GetNewsAPI.tests.test_wikimedia_provider import PayloadClient, page as wikimedia_page


ARTICLE = {
    'id': 6657,
    'title': 'BNB Chain Publishes Litepaper for New Layer-1 Network',
    'seo_focus': 'BNB Chain new Layer 1',
    'full_text': '<p>BNB Chain published a litepaper for a proposed Layer-1 network.</p>',
    'hashtags': 'BNB Chain, Layer 1',
    'category': 'Altcoins',
}
BRIDGE_PAGE = 'https://pixabay.com/photos/szechenyi-chain-bridge-1758196/'
BRIDGE_TAG = 'Szechenyi Chain Bridge'
CREATOR = 'Fixture Contributor'
WIKIMEDIA_FILENAME = 'DSC_0421 (cropped).jpg'
WIKIMEDIA_DESCRIPTION = 'A suspension bridge spanning the Danube at night.'
MEDIA_ID = 6658
BASELINE_QUERIES = [
    'bnb chain new layer', 'bnb chain publishes litepaper new', 'blockchain finance',
]


def bridge_hit() -> dict[str, Any]:
    '''Use the public photo ID and tag with a representative, offline API response.'''
    # The historical search response is unavailable. The public asset page lists the bridge tag.
    return {
        **pixabay_hit(1758196, f'{BRIDGE_TAG}, Budapest, bridge'),
        'pageURL': BRIDGE_PAGE,
    }


def wikimedia_hit(object_name: str, description: str | None) -> dict[str, Any]:
    '''Supply normal Commons search metadata for a consistent file identity.'''
    raw = wikimedia_page()
    raw['title'] = f'File:{WIKIMEDIA_FILENAME}'
    filename = quote(WIKIMEDIA_FILENAME.replace(' ', '_'), safe='()')
    info = raw['imageinfo'][0]
    info.update(
        canonicaltitle=raw['title'],
        url=f'https://upload.wikimedia.org/wikipedia/commons/a/ab/{filename}',
        descriptionurl=f'https://commons.wikimedia.org/wiki/File:{filename}',
    )
    fields = info['extmetadata']
    fields['ObjectName']['value'] = object_name
    if description is None:
        fields.pop('ImageDescription')
    else:
        fields['ImageDescription']['value'] = description
    return raw


class StockPhotoAltTests(unittest.TestCase):
    def upload(
        self, stack: ExitStack, article: dict[str, Any] = ARTICLE,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        '''Capture the real WordPress media-create and media-details requests.'''
        stack.enter_context(patch(
            'requests.sessions.Session.request', side_effect=AssertionError('unexpected network request'),
        ))
        response = Mock(status_code=201)
        response.json.return_value = {'id': MEDIA_ID}
        post = stack.enter_context(patch.object(publisher.session, 'post', return_value=response))
        media_id, metadata = publisher.upload_image(
            None, article['title'], article['hashtags'], article=article,
        )
        self.assertEqual(media_id, MEDIA_ID)
        self.assertEqual(post.call_count, 2)
        upload, update = post.call_args_list
        self.assertTrue(upload.args[0].endswith('/wp-json/wp/v2/media'))
        self.assertTrue(update.args[0].endswith(f'/wp-json/wp/v2/media/{MEDIA_ID}'))
        self.assertTrue(upload.kwargs['data'])
        return metadata, update.kwargs['json']

    def publish_stock(
        self, item: dict[str, Any], *, provider: str = 'pixabay', engine: str = 'v2',
        threshold: float = 0.0,
    ) -> tuple[dict[str, Any], dict[str, str], FixtureResponses | PayloadClient, Mock]:
        '''Run stock adapters, selection, hero cropping, and media POSTs offline.'''
        client = (
            PayloadClient({'query': {'pages': [item]}}) if provider == 'wikimedia'
            else FixtureResponses({'pixabay': 'hits', 'pexels': 'photos'}[provider], [item])
        )
        jpeg = fixture_jpeg('green')
        digest = hashlib.sha256(jpeg).hexdigest()
        with ExitStack() as stack:
            for name, value in {
                'IMAGE_SEARCH_ENGINE': engine, 'IMAGE_SOURCE_MODE': 'stock',
                'IMAGE_SOURCE_PRIORITY': 'stock_first', 'USE_SOURCE_IMAGES': False,
                'STOCK_IMAGE_VISION_MODE': 'off', 'IMAGE_EMERGENCY_STOCK_RESCUE_ENABLED': True,
            }.items():
                stack.enter_context(patch.object(publisher, name, value))
            stack.enter_context(patch.object(
                publisher, '_generated_image_content', side_effect=AssertionError('unexpected image generation'),
            ))

            if engine == 'v2':
                adapter = {
                    'pixabay': PixabayProvider, 'pexels': PexelsProvider, 'wikimedia': WikimediaProvider,
                }[provider]
                credentials = {} if provider == 'wikimedia' else {'api_key': 'fixture'}
                stack.enter_context(patch.object(selection, 'STOCK_IMAGE_SUBJECT_MODE', 'shadow'))
                stack.enter_context(patch.object(emergency, 'STOCK_IMAGE_SUBJECT_MODE', 'shadow'))
                stack.enter_context(patch.object(selection, 'load_image_usage', return_value=[]))
                stack.enter_context(patch.object(selection, 'load_recent_wp_image_usage', return_value=[]))
                stack.enter_context(patch.object(
                    registry, adapter.__name__,
                    partial(adapter, enabled=True, threshold=threshold, **credentials),
                ))
                stack.enter_context(patch.object(
                    selection, 'build_provider_registry',
                    partial(registry.build_provider_registry, (provider,), client=client),
                ))
                download = stack.enter_context(patch.object(
                    ImageDownloader, 'download', autospec=True,
                    return_value=DownloadedImage(jpeg, 'image/jpeg', 1800, 800, digest, digest[:16]),
                ))
            else:
                for name, value in {
                    'PIXABAY_ENABLED': provider == 'pixabay', 'PEXELS_ENABLED': provider == 'pexels',
                    'PIXABAY_API_KEY': 'fixture', 'PEXELS_API_KEY': 'fixture',
                    'PIXABAY_MIN_SCORE': threshold, 'PEXELS_MIN_SCORE': threshold,
                    'STOCK_IMAGE_SUBJECT_MODE': 'shadow',
                }.items():
                    stack.enter_context(patch.object(stock_images, name, value))
                stack.enter_context(patch.object(stock_images, 'load_stock_image_usage', return_value=[]))
                stack.enter_context(patch.object(stock_images, 'load_recent_wp_stock_usage', return_value=[]))
                stack.enter_context(patch.object(
                    stock_images, '_get_json', side_effect=partial(client.get_json, payload_validator=bool),
                ))
                download = stack.enter_context(patch.object(
                    publisher.requests, 'get', return_value=Mock(content=jpeg),
                ))
            metadata, details = self.upload(stack)
        return metadata, details, client, download

    def test_post_6657_uses_visual_tag_and_keeps_selection_downloads_and_credit(self) -> None:
        hit = bridge_hit()
        self.assertNotIn('alt', hit)
        metadata, details, client, download = self.publish_stock(hit)
        self.assertEqual(metadata['asset_key'], 'pixabay:1758196')
        self.assertTrue(metadata['strict_stock_selected'])
        self.assertEqual(metadata['query'], BASELINE_QUERIES[0])
        self.assertEqual(client.calls, [('pixabay', query) for query in BASELINE_QUERIES])
        download.assert_called_once()
        self.assertEqual(download.call_args.args[1].usable_url, hit['largeImageURL'])
        credit = f'Image by {CREATOR} on Pixabay: {BRIDGE_PAGE}'
        self.assertEqual(details['caption'], credit)
        self.assertEqual(details['description'], credit)
        self.assertEqual(metadata['credit_text'], credit)
        self.assertEqual(details['alt_text'], BRIDGE_TAG)

    def test_alt_changes_with_pixabay_photo_and_is_empty_without_subject(self) -> None:
        for hit, expected in (
            (bridge_hit(), BRIDGE_TAG),
            (pixabay_hit(88881, 'Alpine lake at sunrise, mountain, outdoors'), 'Alpine lake at sunrise'),
            (pixabay_hit(88882, ''), ''),
        ):
            asset_id = hit['id']
            with self.subTest(asset_id=asset_id):
                metadata, details, client, download = self.publish_stock(hit)
                self.assertEqual(metadata['asset_key'], f'pixabay:{asset_id}')
                self.assertEqual(details['alt_text'], expected)
                self.assertEqual(client.calls, [('pixabay', query) for query in BASELINE_QUERIES])
                download.assert_called_once()

    def test_pixabay_composition_tags_do_not_displace_a_subject_or_fill_empty_alt(self) -> None:
        for tags, expected in (
            ('Bitcoin, copy space, finance', 'Bitcoin'),
            ('copy space, horizontal', ''),
            ('Horizontal bands of rock, landscape', 'Horizontal bands of rock'),
        ):
            for engine in ('v1', 'v2'):
                with self.subTest(engine=engine, tags=tags):
                    metadata, details, _client, download = self.publish_stock(
                        pixabay_hit(88883, tags), engine=engine,
                    )
                    self.assertEqual(metadata['asset_key'], 'pixabay:88883')
                    download.assert_called_once()
                    if engine == 'v2':
                        self.assertEqual(download.call_args.args[1].subject_text, tags)
                    self.assertEqual(details['alt_text'], expected)

    def test_wikimedia_description_and_object_name_are_separate_from_attribution_and_ranking(self) -> None:
        for object_name, description, expected in (
            (WIKIMEDIA_FILENAME, WIKIMEDIA_DESCRIPTION, WIKIMEDIA_DESCRIPTION),
            (WIKIMEDIA_FILENAME, None, ''),
            ('Danube suspension bridge by moonlight.', None, 'Danube suspension bridge by moonlight.'),
            ('Danube at night', WIKIMEDIA_DESCRIPTION, WIKIMEDIA_DESCRIPTION),
            ('Danube at night', 'Untitled work', 'Danube at night'),
        ):
            with self.subTest(object_name=object_name, description=description):
                raw = wikimedia_hit(object_name, description)
                metadata, details, client, download = self.publish_stock(raw, provider='wikimedia')
                self.assertEqual(metadata['asset_key'], 'wikimedia:123')
                self.assertTrue(metadata['strict_stock_selected'])
                self.assertEqual([args[:2] for args, _ in client.calls], [
                    ('wikimedia', query) for query in BASELINE_QUERIES
                ])
                download.assert_called_once()
                candidate = download.call_args.args[1]
                self.assertEqual(candidate.subject_text, ' '.join(filter(None, (object_name, description))))
                self.assertEqual(
                    candidate.metadata['metadata_text'],
                    ' '.join((raw['title'], object_name, description or '')),
                )
                self.assertEqual(candidate.metadata['work_title'], object_name)
                self.assertEqual(metadata['work_title'], object_name)
                self.assertIn(object_name, details['caption'])
                self.assertIn('CC BY 4.0', details['description'])
                self.assertEqual(details['alt_text'], expected)

    def test_pexels_supplied_alt_reaches_wordpress_in_both_engines(self) -> None:
        long_description = 'White lighthouse on a cliff overlooking the sea. ' * 4
        for alt, expected in (
            ('<span>White  lighthouse&nbsp;on a cliff</span> . Photo by an artist.', 'White lighthouse on a cliff.'),
            (long_description, long_description[:120]),
            ('IMG_1234.JPG', ''),
        ):
            photo = pexels_photo(1719, alt)
            for engine in ('v1', 'v2'):
                with self.subTest(engine=engine, alt=alt):
                    metadata, details, client, download = self.publish_stock(photo, provider='pexels', engine=engine)
                    self.assertEqual(metadata['asset_key'], 'pexels:1719')
                    self.assertEqual(details['alt_text'], expected)
                    self.assertEqual(details['caption'], f"Photo by {CREATOR} on Pexels: {photo['url']}")
                    self.assertEqual(details['description'], details['caption'])
                    self.assertEqual(client.calls[0], ('pexels', BASELINE_QUERIES[0]))
                    download.assert_called_once()

    def test_v1_pixabay_and_emergency_v2_preserve_the_photo_and_its_credit(self) -> None:
        hit = bridge_hit()
        for engine, threshold in (('v1', 0.0), ('v2', 1.0)):
            with self.subTest(engine=engine):
                metadata, details, client, download = self.publish_stock(hit, engine=engine, threshold=threshold)
                self.assertEqual(metadata['asset_key'], 'pixabay:1758196')
                self.assertEqual(details['alt_text'], BRIDGE_TAG)
                self.assertEqual(details['caption'], f'Image by {CREATOR} on Pixabay: {BRIDGE_PAGE}')
                self.assertEqual(details['description'], details['caption'])
                if engine == 'v1':
                    self.assertEqual(download.call_args.args[0], hit['largeImageURL'])
                    self.assertEqual(client.calls, [('pixabay', BASELINE_QUERIES[0])])
                else:
                    self.assertFalse(metadata['strict_stock_selected'])
                    self.assertTrue(metadata['emergency_selected'])
                    self.assertEqual(metadata['emergency_download_attempts'], 1)
                    self.assertEqual(download.call_args.args[1].usable_url, hit['largeImageURL'])
                    self.assertEqual(client.calls, [('pixabay', query) for query in BASELINE_QUERIES])
                download.assert_called_once()

    def test_source_and_generated_images_keep_existing_article_alt(self) -> None:
        jpeg = fixture_jpeg('green')
        for provider, mode, seam, seo_focus in (
            ('source', 'source', '_maybe_source_image_content', ARTICLE['seo_focus']),
            ('openai', 'openai', '_generated_image_content', ''),
        ):
            with self.subTest(provider=provider), ExitStack() as stack:
                stack.enter_context(patch.object(publisher, 'IMAGE_SOURCE_MODE', mode))
                stack.enter_context(patch.object(publisher, 'IMAGE_SOURCE_PRIORITY', 'stock_first'))
                stack.enter_context(patch.object(
                    publisher, seam, return_value=(jpeg, 'fixture.jpg', {'provider': provider}),
                ))
                metadata, details = self.upload(stack, {**ARTICLE, 'seo_focus': seo_focus})
                self.assertEqual(metadata['provider'], provider)
                self.assertEqual(details, {'alt_text': seo_focus or ARTICLE['title']})
