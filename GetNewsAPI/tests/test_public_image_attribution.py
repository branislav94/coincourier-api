"""Public CC credit stays concise; full attribution remains in the audit trail."""
from __future__ import annotations

import html
from html.parser import HTMLParser
import unittest
from unittest.mock import Mock, patch

from image_search.models import DownloadedImage
from image_search.providers.openverse import OpenverseProvider
from image_search.providers.wikimedia import WikimediaProvider
from image_search.reuse import record_image_usage
from publishing.wordpress import media, publisher
from publishing.wordpress.attribution import build_public_attribution


SOURCE = "https://commons.wikimedia.org/wiki/File:Solana_blockchain_illustration.jpg"
TITLE = "Solana blockchain illustration"
CHANGE_NOTICE = "\nChanges: cropped and resized for publication."


class Caption(HTMLParser):
    def __init__(self, value: str):
        super().__init__(convert_charrefs=True)
        self.text_parts, self.tags, self.links = [], [], []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "a":
            self.links.append(dict(attrs).get("href"))

    def handle_data(self, value):
        self.text_parts.append(value)

    @property
    def text(self):
        return " ".join(self.text_parts)


def wikimedia(version="2.0", *, notice="", credit="Own work"):
    values = {
        "Artist": '<a href="https://creator.example/paul">Paul Williamson</a>',
        "Credit": credit, "Attribution": notice, "ObjectName": TITLE,
        "ImageDescription": "Solana blockchain illustration", "LicenseShortName": f"CC BY {version}",
        "LicenseUrl": f"https://creativecommons.org/licenses/by/{version}/", "UsageTerms": f"CC BY {version}",
        "Copyrighted": "True", "AttributionRequired": "true",
    }
    item = WikimediaProvider(enabled=False).normalize_candidate({
        "pageid": 11, "ns": 6, "title": f"File:{TITLE}.jpg",
        "imageinfo": [{
            "canonicaltitle": f"File:{TITLE}.jpg", "width": 1600, "height": 900, "size": 1000,
            "mime": "image/jpeg", "url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Solana_blockchain_illustration.jpg",
            "descriptionurl": SOURCE, "descriptionshorturl": "https://commons.wikimedia.org/?curid=11",
            "extmetadata": {key: {"value": value} for key, value in values.items()},
        }],
    }, "solana blockchain", 0)
    assert item is not None
    return item


def prepared(item):
    downloaded = DownloadedImage(b"fixture", "image/jpeg", 1600, 900, "sha", "0123456789abcdef")
    return publisher._candidate_stock_content(item, downloaded, (b"hero", {"width": 1536, "height": 1024}))


def metadata(version="2.0"):
    return prepared(wikimedia(version))[2]


class PublicAttributionTests(unittest.TestCase):
    def assert_plain_caption(self, caption):
        self.assertEqual(Caption(caption).tags, [])
        for forbidden in ("<", ">", "href=", "rel=", "http://", "https://", "//", "www.", "&lt;", "&gt;", "&amp;",
                          "File:", "Artist:", "Credit:", "Attribution:", "Source:", "License:", "Changes:",
                          "wikimedia:", "openverse:", "identity_aliases"):
            self.assertNotIn(forbidden, caption)

    def upload(self, selected):
        response = Mock()
        with (
            patch.object(publisher, "upload_media", return_value=77) as upload,
            patch.object(publisher.session, "post", return_value=response) as post,
        ):
            result = publisher._upload_prepared_image(selected, {"title": "Solana blockchain article"}, {"mode": "hybrid"})
        payload = post.call_args.kwargs["json"] if post.called else None
        return result, payload, upload

    def test_compact_caption_and_complete_description_are_separate_wordpress_fields(self):
        item = wikimedia(notice="Please credit Paul Williamson.")
        original = item.attribution_text
        (media_id, result), payload, _upload = self.upload(prepared(item))
        self.assertEqual(media_id, 77)
        caption = payload["caption"]
        self.assert_plain_caption(caption)
        for component in ("Paul Williamson", TITLE, "Wikimedia Commons", "CC BY 2.0", "Cropped", "Please credit Paul Williamson"):
            self.assertIn(component, caption)
        self.assertIn(SOURCE, html.unescape(payload["description"]))
        self.assertIn(item.license_url, html.unescape(payload["description"]))
        self.assertNotEqual(payload["caption"], payload["description"])
        self.assertEqual(html.unescape(payload["description"]), html.unescape(original + CHANGE_NOTICE))
        self.assertEqual(result["provider_attribution_text"], original)
        self.assertEqual(result["attribution_text"], original + CHANGE_NOTICE)
        self.assertEqual(result["credit_text"], original + CHANGE_NOTICE)
        self.assertEqual(item.attribution_text, original)
        self.assertEqual(result["public_attribution_caption"], payload["caption"])

    def test_public_caption_does_not_show_audit_labels_urls_or_internal_identities(self):
        (_media_id, _result), payload, _upload = self.upload(prepared(wikimedia()))
        self.assert_plain_caption(payload["caption"])

    def test_old_cc_by_versions_preserve_supplied_title_and_exact_license(self):
        for version in ("2.0", "2.5", "3.0", "4.0"):
            with self.subTest(version=version):
                item = wikimedia(version)
                caption, description = build_public_attribution(prepared(item)[2], item.attribution_text + CHANGE_NOTICE)
                self.assert_plain_caption(caption)
                self.assertIn(TITLE, caption)
                self.assertIn(f"CC BY {version}", caption)
                self.assertIn(item.license_url, html.unescape(description))
                self.assertIn(SOURCE, html.unescape(description))

    def test_real_structured_creator_name_does_not_display_embedded_creator_url(self):
        item = wikimedia()
        self.assertIn("https://creator.example/paul", item.creator_name)
        values = prepared(item)[2]
        self.assertEqual(values["creator_display_name"], "Paul Williamson")
        caption, description = build_public_attribution(values, item.attribution_text + CHANGE_NOTICE)
        self.assert_plain_caption(caption)
        self.assertIn("Paul Williamson", caption)
        self.assertIn("https://creator.example/paul", html.unescape(description))

    def test_required_custom_uri_cannot_be_silently_removed_for_plain_caption(self):
        values = metadata()
        values["provider"] = "openverse"
        values["source_page_url"] = "https://source.example/work"
        for uri in ("https://source.example/work/credits", "//archive.example/credit", "www.archive.example/credit",
                    "https&#58;//archive.example/credit"):
            with self.subTest(uri=uri):
                values["attribution_notice"] = "Please credit here " + uri
                full = "Complete required credit. " + values["attribution_notice"]
                with self.assertRaises(media.MediaMetadataError) as caught:
                    build_public_attribution(values, full)
                self.assertEqual(caught.exception.reason, "attribution_missing")
                self.assertIn(uri, full)

    def test_source_urls_remain_in_description_without_giant_visible_url_text(self):
        values = metadata()
        values["provider"] = "openverse"
        values["source_page_url"] = "https://source.example/" + "long-source-path-" * 100
        caption, description = build_public_attribution(values, "Complete credit")
        self.assert_plain_caption(caption)
        self.assertIn(values["source_page_url"], html.unescape(description))
        self.assertIn(values["license_url"], html.unescape(description))
        self.assertNotIn("long-source-path", caption)

    def test_supplied_unrepresentable_title_fails_closed_instead_of_disappearing(self):
        for title in ("https://source.example/work-title", "Map at https://title.example/reference", "<script>supplied-title</script>",
                      "File:", "Value > cost", "Artist: supplied title", "wikimedia:11", "www.example.org/title", "//example.org/title"):
            with self.subTest(title=title):
                values = metadata()
                values["work_title"] = title
                with self.assertRaises(media.MediaMetadataError):
                    build_public_attribution(values, "Complete required credit")

    def test_full_long_credit_survives_without_public_caption_dump(self):
        item = wikimedia()
        values = prepared(item)[2]
        full = item.attribution_text + " Provider audit provenance." * 100 + CHANGE_NOTICE
        caption, description = build_public_attribution(values, full)
        self.assertGreater(len(full), 1000)
        self.assertEqual(html.unescape(description), html.unescape(full))
        self.assert_plain_caption(caption)
        self.assertNotIn("Provider audit provenance", caption)

    def test_required_custom_credit_and_copyright_notice_are_preserved(self):
        item = wikimedia(notice="© 2020 Paul Williamson and Museum Trust. Please credit Museum Trust.", credit="Museum Trust archive")
        caption, description = build_public_attribution(prepared(item)[2], item.attribution_text + CHANGE_NOTICE)
        self.assert_plain_caption(caption)
        self.assertIn("© 2020 Paul Williamson and Museum Trust", caption)
        self.assertIn("Please credit Museum Trust", caption)
        self.assertIn("Museum Trust archive", caption)
        self.assertIn("Museum Trust", html.unescape(description))

    def test_unrepresentable_custom_credit_is_rejected_not_truncated(self):
        values = metadata()
        values["attribution_notice"] = "Required named party and notice. " * 30
        with self.assertRaises(media.MediaMetadataError) as caught:
            build_public_attribution(values, "Complete provider credit. " + values["attribution_notice"])
        self.assertEqual(caught.exception.reason, "attribution_too_large")

    def test_html_is_safe_in_caption_and_complete_attachment_description(self):
        values = metadata()
        values["creator_display_name"] = 'Paul<img src=x onerror="steal()"> &amp; Team'
        values["work_title"] = '<script>steal()</script>Solana <b>blockchain</b>'
        values["attribution_notice"] = '© 2020 Paul <img src=x onerror="steal()"> and Team'
        full = '<a href="https://source.example/work">Required credit</a><script>steal()</script><img src=x onerror="steal()"> © 2020 Paul and Team' + CHANGE_NOTICE
        caption, description = build_public_attribution(values, full)
        self.assert_plain_caption(caption)
        self.assertNotIn("steal()", caption)
        self.assertIn("Paul & Team", caption)
        self.assertIn("Solana blockchain", caption)
        self.assertIn("© 2020 Paul and Team", caption)
        self.assertEqual(Caption(description).tags, [])
        self.assertTrue(html.unescape(description).startswith(html.unescape(full)))
        self.assertIn(SOURCE, html.unescape(description))
        self.assertIn(values["license_url"], html.unescape(description))

    def test_encoded_markup_is_not_reactivated_and_commons_text_is_not_double_escaped(self):
        values = metadata()
        for creator in ("Paul &amp; Team &lt;img src=x onerror=bad&gt;", "Paul &amp;amp; Team &amp;lt;script&amp;gt;bad&amp;lt;/script&amp;gt;"):
            with self.subTest(creator=creator):
                values["creator_display_name"] = creator
                full = "Paul &amp; Team &lt;img src=x onerror=bad&gt; Source: https://source.example/work"
                caption, description = build_public_attribution(values, full)
                self.assert_plain_caption(caption)
                self.assertIn("Paul & Team", caption)
                self.assertNotIn("onerror", caption)
                self.assertNotIn("bad", caption)
                self.assertNotIn("&amp;amp;", description)
                self.assertEqual(Caption(description).tags, [])
                self.assertTrue(html.unescape(description).startswith(html.unescape(full)))

    def test_invalid_source_or_license_link_fails_closed(self):
        for field, value in (("source_page_url", "javascript:alert(1)"), ("source_page_url", "http://127.0.0.1/work"),
                             ("source_page_url", "http://%31%32%37.0.0.1/work"), ("source_page_url", "http://127.1/work"),
                             ("source_page_url", "https://invalid.%ZZ.example/work"), ("source_page_url", "https://evil.<bad>.example/work"),
                             ("license_url", "https://creativecommons.org.evil.example/licenses/by/2.0/"),
                             ("license_url", "https://creativecommons.org/licenses/by-sa/2.0/")):
            with self.subTest(field=field, value=value):
                values = metadata()
                values["provider"] = "openverse"
                values[field] = value
                with self.assertRaises(media.MediaMetadataError):
                    build_public_attribution(values, "Complete credit")

    def test_missing_required_attribution_prevents_upload(self):
        selected = prepared(wikimedia())
        selected[2]["credit_text"] = selected[2]["attribution_text"] = ""
        (media_id, result), payload, upload = self.upload(selected)
        self.assertIsNone(media_id)
        self.assertEqual(result["featured_image_rejected_reason"], "attribution_missing")
        self.assertIsNone(payload)
        upload.assert_not_called()

    def test_unknown_creator_authoritative_credit_preserves_designated_party(self):
        source = "https://www.flickr.com/photos/fixture/11/"
        license_url = "https://creativecommons.org/licenses/by/2.0/"
        authoritative = f'"{TITLE}" by Archive Museum is licensed under <a href="{license_url}">CC BY 2.0</a>. Source: <a href="{source}">{TITLE}</a>.'
        item = OpenverseProvider(enabled=False).normalize_candidate({
            "id": "ov-11", "source": "flickr", "url": "https://live.staticflickr.com/1234/fixture.jpg",
            "foreign_landing_url": source, "creator": "Unknown", "license": "by", "license_version": "2.0",
            "license_url": license_url, "title": TITLE, "attribution": authoritative,
        }, "solana blockchain", 0)
        assert item is not None
        self.assertTrue(item.metadata["attribution_authoritative"])
        (_media_id, result), payload, _upload = self.upload(prepared(item))
        self.assert_plain_caption(payload["caption"])
        self.assertIn("Archive Museum", payload["caption"])
        self.assertIn(TITLE, payload["caption"])
        self.assertIn(source, html.unescape(payload["description"]))
        self.assertIn(license_url, html.unescape(payload["description"]))
        self.assertEqual(result["provider_attribution_text"], authoritative)

    def test_unknown_creator_without_representable_party_fails_closed(self):
        values = metadata()
        values.update(creator_name="Unknown", creator_display_name="", attribution_authoritative=True,
                      provider_attribution_text=f"{TITLE}. Source: {SOURCE}. License: CC BY 2.0 ({values['license_url']})")
        with self.assertRaises(media.MediaMetadataError):
            build_public_attribution(values, values["provider_attribution_text"] + CHANGE_NOTICE)

    def test_pexels_and_pixabay_public_credit_behavior_is_unchanged(self):
        for provider in ("pexels", "pixabay"):
            with self.subTest(provider=provider):
                original = f"Existing {provider} credit. " * 30
                (media_id, result), payload, _upload = self.upload((b"photo", "stock.jpg", {"provider": provider, "credit_text": original}))
                self.assertEqual(media_id, 77)
                self.assertEqual(payload["caption"], original)
                self.assertEqual(payload["description"], original)
                self.assertEqual(result["credit_text"], original)
                self.assertNotIn("public_attribution_caption", result)

    def test_cc0_and_pdm_use_compact_best_effort_credit(self):
        for name, url in (("cc0", "https://creativecommons.org/publicdomain/zero/1.0/"), ("pdm", "https://creativecommons.org/publicdomain/mark/1.0/")):
            with self.subTest(name=name):
                values = metadata()
                values.update(license_name=name, license_version="1.0", license_url=url, creator_name="", creator_display_name="")
                caption, description = build_public_attribution(values, "Full public-domain provider audit")
                self.assert_plain_caption(caption)
                self.assertIn(name.upper(), caption)
                self.assertIn(url, html.unescape(description))
                self.assertIn(SOURCE, html.unescape(description))
                self.assertTrue(description.startswith("Full public-domain provider audit"))

    def test_public_domain_long_optional_credits_do_not_reject_valid_asset(self):
        for name, url in (("cc0", "https://creativecommons.org/publicdomain/zero/1.0/"), ("pdm", "https://creativecommons.org/publicdomain/mark/1.0/")):
            with self.subTest(name=name):
                values = metadata()
                title = "Long optional supplied title " * 25
                notice = "Long optional public-domain credit " * 25
                full = f"{title}. {notice}. Source: {SOURCE}. License: {name.upper()} ({url})"
                values.update(license_name=name, license_version="1.0", license_url=url,
                              work_title=title, attribution_notice=notice, creator_display_name="Optional creator " * 30,
                              credit="Optional credit https://invalid.%ZZ.example/notice")
                values["credit_text"] = values["attribution_text"] = full
                (media_id, result), payload, _upload = self.upload((b"image", "public-domain.jpg", values))
                self.assertEqual(media_id, 77)
                self.assert_plain_caption(payload["caption"])
                self.assertIn(SOURCE, html.unescape(payload["description"]))
                self.assertIn(url, html.unescape(payload["description"]))
                self.assertIn(name.upper(), payload["caption"])
                self.assertNotIn("Long optional", payload["caption"])
                self.assertEqual(result["provider_attribution_text"], full)
                self.assertEqual(result["attribution_text"], full)
                self.assertTrue(html.unescape(payload["description"]).startswith(html.unescape(full)))
        values = metadata()
        values.update(license_name="cc0", license_url="https://creativecommons.org/publicdomain/zero/1.0/", work_title="Map at https://title.example/reference")
        caption, description = build_public_attribution(values, "Complete public-domain audit")
        self.assert_plain_caption(caption)
        self.assertIn(SOURCE, html.unescape(description))

    def test_frontend_rendering_caption_as_strict_text_displays_clean_credit(self):
        values = metadata()
        values.update(creator_name="edwinchuen", creator_display_name="edwinchuen",
                      work_title="Bitcoin Cryptocurrency Laws & Regulations")
        full = f"Bitcoin Cryptocurrency Laws &amp; Regulations. Artist: edwinchuen. Source: {SOURCE}. License: CC BY 2.0 ({values['license_url']})"
        values["credit_text"] = values["attribution_text"] = full
        (media_id, result), payload, _upload = self.upload((b"photo", "bitcoin.jpg", values))
        expected = "Photo: edwinchuen · “Bitcoin Cryptocurrency Laws & Regulations” · Wikimedia Commons · CC BY 2.0 · Cropped"
        self.assertEqual(media_id, 77)
        self.assertEqual(payload["caption"], expected)
        self.assertEqual(str(payload["caption"]), expected)
        self.assertEqual(result["public_attribution_caption"], expected)
        self.assert_plain_caption(expected)
        self.assertEqual(result["credit_text"], full + CHANGE_NOTICE)
        self.assertEqual(result["attribution_text"], full + CHANGE_NOTICE)
        self.assertEqual(result["provider_attribution_text"], full)
        self.assertEqual(html.unescape(payload["description"]), html.unescape(full + CHANGE_NOTICE))

    def test_required_notice_must_already_be_retained_in_complete_audit(self):
        values = metadata()
        values["attribution_notice"] = "Please credit the Archive Foundation."
        with self.assertRaises(media.MediaMetadataError) as caught:
            build_public_attribution(values, "Unrelated complete credit")
        self.assertEqual(caught.exception.reason, "attribution_missing")

    def test_structural_uri_within_required_notice_preserves_plain_notice_and_full_uri(self):
        values = metadata()
        values["attribution_notice"] = f"Please credit Paul {SOURCE} here."
        full = "Complete credit. " + values["attribution_notice"]
        caption, description = build_public_attribution(values, full)
        self.assert_plain_caption(caption)
        self.assertIn("Please credit Paul here", caption)
        self.assertIn(values["attribution_notice"], html.unescape(description))

    def test_missing_standard_uris_are_retained_exactly_not_by_prefix_match(self):
        values = metadata()
        values.update(provider="openverse", source_page_url="https://source.example/work")
        full = f"Source: {values['source_page_url']}/other. License: {values['license_url']}deed.en"
        caption, description = build_public_attribution(values, full)
        self.assert_plain_caption(caption)
        self.assertIn("\nSource: https://source.example/work\n", html.unescape(description))
        self.assertIn(f"\nLicense: CC BY 2.0 ({values['license_url']})", html.unescape(description))

    def test_description_additions_cannot_bypass_complete_audit_size_bound(self):
        values = metadata()
        with self.assertRaises(media.MediaMetadataError) as caught:
            build_public_attribution(values, "x" * 16380)
        self.assertEqual(caught.exception.reason, "attribution_too_large")

    def test_generated_images_have_no_false_cc_crop_caption(self):
        (media_id, result), payload, _upload = self.upload((b"generated", "grok.jpg", {"provider": "grok-image", "credit_text": ""}))
        self.assertEqual(media_id, 77)
        self.assertNotIn("caption", payload)
        self.assertNotIn("description", payload)
        self.assertNotIn("public_attribution_caption", result)

    def test_complete_credit_is_still_recorded_for_reuse(self):
        item = wikimedia()
        (_media_id, result), _payload, _upload = self.upload(prepared(item))
        with patch("image_search.reuse.load_image_usage", return_value=[]), patch("image_search.reuse._write_image_usage") as write:
            record_image_usage(result, 101, "Article")
        entry = write.call_args.args[0][0]
        self.assertEqual(entry["attribution_text"], item.attribution_text + CHANGE_NOTICE)
        self.assertEqual(entry["canonical_source"], item.canonical_source)
        self.assertEqual(entry["license_url"], item.license_url)


if __name__ == "__main__":
    unittest.main()
