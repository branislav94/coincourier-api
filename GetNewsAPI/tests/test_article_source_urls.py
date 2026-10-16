'''Offline coverage for source permissions and conservative outbound cleanup.'''

from __future__ import annotations

from copy import deepcopy
import unittest

from GetNewsAPI.article_source_urls import (
    clean_approved_article_links,
    extract_source_urls,
    normalize_source_url,
)


BENZINGA_URL = (
    'https://www.benzinga.com/crypto/cryptocurrency/26/10/62351460/'
    'is-a-stronger-dollar-bullish-for-bitcoin'
)


class SourceUrlNormalizationTests(unittest.TestCase):
    def test_actual_benzinga_tracking_parameter_is_removed(self) -> None:
        self.assertEqual(normalize_source_url(BENZINGA_URL + '?utm_source=snapi'), BENZINGA_URL)

    def test_url_without_tracking_is_unchanged(self) -> None:
        url = 'https://issuer.example.invalid/report/2026-10-15?instrument=BTC&edition=2'
        self.assertEqual(normalize_source_url(url), url)

    def test_meaningful_query_bytes_order_duplicates_and_blanks_are_preserved(self) -> None:
        url = (
            'https://issuer.example.invalid/report?q=a%20b&q=a+b&empty=&flag&'
            'utm_source=snapi&value=%2f%2F&ref=feed&last=1#section'
        )
        expected = 'https://issuer.example.invalid/report?q=a%20b&q=a+b&empty=&flag&value=%2f%2F&last=1'
        self.assertEqual(normalize_source_url(url), expected)

    def test_encoded_and_case_variant_tracking_keys_are_recognized(self) -> None:
        url = 'https://issuer.example.invalid/report?%75tm_source=snapi&GCLID=abc&FBCLID=def&id=12'
        self.assertEqual(normalize_source_url(url), 'https://issuer.example.invalid/report?id=12')

    def test_tracking_does_not_remove_similarly_named_meaningful_identifiers(self) -> None:
        url = 'https://issuer.example.invalid/report?reference=1&utmish=2&ref_id=3&id=4'
        self.assertEqual(normalize_source_url(url), url)

    def test_empty_query_marker_and_empty_segments_are_preserved(self) -> None:
        self.assertEqual(normalize_source_url('https://issuer.example.invalid/report?'), 'https://issuer.example.invalid/report?')
        self.assertEqual(
            normalize_source_url('https://issuer.example.invalid/report?a=1&&utm_source=x&b=&'),
            'https://issuer.example.invalid/report?a=1&&b=&',
        )

    def test_signed_and_sensitive_urls_are_preserved_in_full(self) -> None:
        for credential in ('Signature=abc', 'sig=abc', 'X-Amz-Signature=abc', 'X-Goog-Credential=x',
                           'token=x', 'Key-Pair-Id=K123', 'Policy=x', 'Expires=123'):
            with self.subTest(credential=credential):
                url = f'https://issuer.example.invalid/report?utm_source=snapi&{credential}&ref=needed#page'
                self.assertEqual(normalize_source_url(url), url)

    def test_raw_url_normalization_is_idempotent_and_does_not_decode_html_entities(self) -> None:
        for raw in (
            'https://issuer.example.invalid/report?id=12&amp;utm_source=snapi&amp;page=2',
            'https://issuer.example.invalid/report?id=12&amp;amp;utm_source=snapi',
            'https://issuer.example.invalid/report?id=12&utm_source=snapi&page=2',
        ):
            normalized = normalize_source_url(raw)
            self.assertEqual(normalize_source_url(normalized), normalized)
        literal_entity = 'https://issuer.example.invalid/report?id=12&amp;utm_source=snapi&amp;page=2'
        self.assertEqual(normalize_source_url(literal_entity), literal_entity)

    def test_unsafe_or_malformed_inputs_fail_closed(self) -> None:
        for url in (
            None, 12, '', '/internal', '//issuer.example.invalid/report', 'javascript:alert(1)',
            'ftp://issuer.example.invalid/report', 'https:///missing-host',
            'https://user:secret@issuer.example.invalid/report', 'https://user@issuer.example.invalid/report',
            'https://issuer.example.invalid:invalid/report', 'https://issuer.example.invalid:99999/report',
            'https://issuer.example.invalid:/report', 'https://[::1]suffix/report',
            'https://issuer.example.invalid/line\nbreak',
            'https://issuer.example.invalid/\u202ebidi', 'https://issuer.example.invalid/\x80control',
            'https://issuer.example.invalid/a b', 'https://issuer.example.invalid/\\evil',
            'https://issuer.example.invalid/%not-encoded', 'https://bad_host.example.invalid/report',
            'https://%65xample.invalid/report', 'https://issuer.example.invalid/<script>',
        ):
            with self.subTest(url=url):
                self.assertIsNone(normalize_source_url(url))

    def test_valid_ipv6_and_idna_hosts_remain_available(self) -> None:
        for url in ('https://[2001:db8::1]:443/report', 'https://bücher.example/report'):
            with self.subTest(url=url):
                self.assertEqual(normalize_source_url(url), url)

    def test_wikipedia_hosts_are_excluded_without_matching_other_hosts_or_paths(self) -> None:
        for url in ('https://wikipedia.org/report', 'https://en.wikipedia.org/report', 'https://en.wikipedia.org./report'):
            self.assertIsNone(normalize_source_url(url))
        self.assertEqual(
            normalize_source_url('https://issuer.example.invalid/wikipedia.org'),
            'https://issuer.example.invalid/wikipedia.org',
        )


class SourceUrlExtractionTests(unittest.TestCase):
    def test_both_quotes_html_entities_and_duplicates_share_normalization(self) -> None:
        html = (
            '<a href="https://issuer.example.invalid/a?id=1&amp;utm_source=feed">First</a>'
            "<A HREF='https://issuer.example.invalid/a?id=1#section'>Duplicate</A>"
            "<a href='https://issuer.example.invalid/b?fbclid=x'>Second</a>"
        )
        self.assertEqual(extract_source_urls(html), [
            'https://issuer.example.invalid/a?id=1', 'https://issuer.example.invalid/b',
        ])

    def test_only_actual_anchor_href_attributes_are_extracted(self) -> None:
        html = (
            '<!-- <a href="https://comment.example.invalid/">Comment</a> -->'
            '<script>const text = \'<a href="https://script.example.invalid/">\';</script>'
            '<iframe src="https://video.example.invalid/"></iframe>'
            '<a data-href="https://data.example.invalid/" title=\' href="https://title.example.invalid/"\' '
            'href="https://issuer.example.invalid/">Link</a>'
        )
        self.assertEqual(extract_source_urls(html), ['https://issuer.example.invalid/'])

    def test_invalid_relative_and_wikipedia_links_do_not_become_permissions(self) -> None:
        html = (
            '<a href="/internal">Internal</a><a href="https://en.wikipedia.org/wiki/Test">Wiki</a>'
            '<a href="https://user@issuer.example.invalid/">Credentials</a>'
            '<a href="https://issuer.example.invalid:bad/">Malformed</a>'
        )
        self.assertEqual(extract_source_urls(html), [])

    def test_signed_source_links_remain_intact(self) -> None:
        url = 'https://issuer.example.invalid/report?utm_source=feed&sig=a%2Bb'
        self.assertEqual(extract_source_urls(f'<a href="{url}">Report</a>'), [url])

    def test_html_entity_control_characters_are_rejected_after_one_attribute_decode(self) -> None:
        self.assertEqual(extract_source_urls('<a href="https://issuer.example.invalid/&#10;break">Unsafe</a>'), [])

    def test_nested_html_entities_are_decoded_once_without_reinterpreting_literal_query_keys(self) -> None:
        raw = 'https://issuer.example.invalid/report?id=12&amp;utm_source=literal'
        html = '<a href="https://issuer.example.invalid/report?id=12&amp;amp;utm_source=literal">Report</a>'
        self.assertEqual(extract_source_urls(html), [raw])
        self.assertEqual(normalize_source_url(normalize_source_url(raw)), raw)
        self.assertEqual(clean_approved_article_links(html, [raw]), html)


class ApprovedArticleLinkCleanupTests(unittest.TestCase):
    def test_exact_benzinga_href_is_cleaned_without_changing_surrounding_html(self) -> None:
        html = f'<p>Bitcoin, 82,156 on October 15, 2026.</p><a class="source" href="{BENZINGA_URL}?utm_source=snapi">Benzinga</a>'
        self.assertEqual(
            clean_approved_article_links(html, [BENZINGA_URL]),
            html.replace('?utm_source=snapi', ''),
        )

    def test_approved_input_with_tracking_and_output_with_different_tracking_match(self) -> None:
        source = 'https://issuer.example.invalid/report?id=12&utm_source=input'
        html = '<a href="https://issuer.example.invalid/report?id=12&amp;utm_medium=output">Report</a>'
        self.assertEqual(
            clean_approved_article_links(html, [source]),
            '<a href="https://issuer.example.invalid/report?id=12">Report</a>',
        )

    def test_meaningful_query_is_html_escaped_and_single_quote_style_is_retained(self) -> None:
        html = "<a title='Report' href='https://issuer.example.invalid/report?id=12&amp;utm_source=x&amp;page=2'>Source</a>"
        self.assertEqual(
            clean_approved_article_links(html, ['https://issuer.example.invalid/report?id=12&page=2']),
            "<a title='Report' href='https://issuer.example.invalid/report?id=12&amp;page=2'>Source</a>",
        )

    def test_unapproved_internal_video_and_comment_links_are_unchanged(self) -> None:
        html = (
            '<p>Unchanged, 1,200 units.</p>\r\n'
            '<!-- <a href="https://issuer.example.invalid/report?utm_source=x">Comment</a> -->'
            '<script>const text = \'<a href="https://issuer.example.invalid/report?utm_source=x">\';</script>'
            '<a href="https://other.example.invalid/report?utm_source=x">Unapproved</a>'
            '<a href="/internal?utm_source=x">Internal</a>'
            '<iframe src="https://video.example.invalid/embed?id=1&amp;utm_source=x"></iframe>'
            '<a href="https://issuer.example.invalid/report?utm_source=x">Approved</a>'
        )
        expected = html.replace(
            '<a href="https://issuer.example.invalid/report?utm_source=x">Approved</a>',
            '<a href="https://issuer.example.invalid/report">Approved</a>',
        )
        self.assertEqual(clean_approved_article_links(html, ['https://issuer.example.invalid/report']), expected)

    def test_signed_approved_urls_remain_byte_identical(self) -> None:
        url = 'https://issuer.example.invalid/report?utm_source=snapi&amp;sig=a%2Bb#section'
        html = f'<a href="{url}">Signed report</a>'
        self.assertEqual(clean_approved_article_links(html, [url.replace('&amp;', '&')]), html)

    def test_links_already_clean_are_byte_identical(self) -> None:
        html = "<A HREF = 'https://issuer.example.invalid/report?id=1&#38;page=2'>Report</A>"
        self.assertEqual(clean_approved_article_links(html, ['https://issuer.example.invalid/report?id=1&page=2']), html)

    def test_cleanup_does_not_mutate_allowed_source_inputs_or_grant_permissions(self) -> None:
        allowed = ['https://issuer.example.invalid/report?utm_source=snapi']
        before = deepcopy(allowed)
        html = '<a href="https://issuer.example.invalid/report?id=not-approved&utm_source=snapi">Different resource</a>'
        self.assertEqual(clean_approved_article_links(html, allowed), html)
        self.assertEqual(allowed, before)

    def test_apostrophes_are_escaped_only_when_replacing_single_quoted_href(self) -> None:
        url = "https://issuer.example.invalid/report?title=issuer's&page=2"
        html = "<a href='https://issuer.example.invalid/report?title=issuer&#x27;s&amp;page=2&amp;utm_source=x'>Report</a>"
        self.assertEqual(clean_approved_article_links(html, [url]), html.replace('&amp;utm_source=x', ''))


if __name__ == '__main__':
    unittest.main()
