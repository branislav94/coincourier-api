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
        view = Caption(payload["caption"])
        self.assertIn("Paul Williamson", view.text)
        self.assertIn(TITLE, view.text)
        self.assertIn("Wikimedia Commons", view.text)
        self.assertIn("CC BY 2.0", view.text)
        self.assertIn("Cropped", view.text)
        self.assertIn("Please credit Paul Williamson", view.text)
        self.assertIn(SOURCE, view.links)
        self.assertIn(item.license_url, view.links)
        self.assertNotEqual(payload["caption"], payload["description"])
        self.assertEqual(html.unescape(payload["description"]), html.unescape(original + CHANGE_NOTICE))
        self.assertEqual(result["provider_attribution_text"], original)
        self.assertEqual(result["attribution_text"], original + CHANGE_NOTICE)
        self.assertEqual(result["credit_text"], original + CHANGE_NOTICE)
        self.assertEqual(item.attribution_text, original)
        self.assertEqual(result["public_attribution_caption"], payload["caption"])

    def test_public_caption_does_not_show_audit_labels_urls_or_internal_identities(self):
        (_media_id, _result), payload, _upload = self.upload(prepared(wikimedia()))
        text = Caption(payload["caption"]).text
        for value in ("File:", "Artist:", "Credit:", "Source:", "License:", "Changes:", "https://", "http://", "wikimedia:11"):
            self.assertNotIn(value, text)
        self.assertNotIn("identity_aliases", payload["caption"])

    def test_old_cc_by_versions_preserve_supplied_title_and_exact_license(self):
        for version in ("2.0", "2.5", "3.0", "4.0"):
            with self.subTest(version=version):
                item = wikimedia(version)
                caption, _description = build_public_attribution(prepared(item)[2], item.attribution_text + CHANGE_NOTICE)
                view = Caption(caption)
                self.assertIn(TITLE, view.text)
                self.assertIn(f"CC BY {version}", view.text)
                self.assertIn(item.license_url, view.links)

    def test_real_structured_creator_name_does_not_display_embedded_creator_url(self):
        item = wikimedia()
        self.assertIn("https://creator.example/paul", item.creator_name)
        values = prepared(item)[2]
        self.assertEqual(values["creator_display_name"], "Paul Williamson")
        caption, _description = build_public_attribution(values, item.attribution_text + CHANGE_NOTICE)
        self.assertNotIn("https://", Caption(caption).text)
        self.assertIn("Paul Williamson", Caption(caption).text)
        self.assertIn("https://creator.example/paul", Caption(caption).links)

    def test_notice_uri_sharing_source_prefix_is_preserved_as_its_own_link(self):
        values = metadata()
        values["provider"] = "openverse"
        values["source_page_url"] = "https://source.example/work"
        values["attribution_notice"] = "Please credit here https://source.example/work/credits."
        caption, _description = build_public_attribution(values, "Complete required credit")
        view = Caption(caption)
        self.assertIn("https://source.example/work/credits", view.links)
        self.assertIn("Please credit here", view.text)
        self.assertNotIn("/credits", view.text)

    def test_source_urls_remain_links_without_giant_visible_url_text(self):
        values = metadata()
        values["provider"] = "openverse"
        values["source_page_url"] = "https://source.example/" + "long-source-path-" * 100
        caption, _description = build_public_attribution(values, "Complete credit")
        view = Caption(caption)
        self.assertIn(values["source_page_url"], view.links)
        self.assertNotIn("long-source-path", view.text)
        self.assertNotIn("https://", view.text)

    def test_supplied_unrepresentable_title_fails_closed_instead_of_disappearing(self):
        for title in ("https://source.example/work-title", "Map at https://title.example/reference", "<script>supplied-title</script>", "File:"):
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
        self.assertNotIn("Provider audit provenance", Caption(caption).text)

    def test_required_custom_credit_and_copyright_notice_are_preserved(self):
        item = wikimedia(notice="© 2020 Paul Williamson and Museum Trust. Please credit Museum Trust.", credit="Museum Trust archive")
        caption, description = build_public_attribution(prepared(item)[2], item.attribution_text + CHANGE_NOTICE)
        text = Caption(caption).text
        self.assertIn("© 2020 Paul Williamson and Museum Trust", text)
        self.assertIn("Please credit Museum Trust", text)
        self.assertIn("Museum Trust archive", text)
        self.assertIn("Museum Trust", html.unescape(description))

    def test_unrepresentable_custom_credit_is_rejected_not_truncated(self):
        values = metadata()
        values["attribution_notice"] = "Required named party and notice. " * 30
        with self.assertRaises(media.MediaMetadataError) as caught:
            build_public_attribution(values, "Complete provider credit")
        self.assertEqual(caught.exception.reason, "attribution_too_large")

    def test_html_is_safe_in_caption_and_complete_attachment_description(self):
        values = metadata()
        values["creator_display_name"] = 'Paul<img src=x onerror="steal()"> &amp; Team'
        values["work_title"] = '<script>steal()</script>Solana <b>blockchain</b>'
        values["attribution_notice"] = '© 2020 Paul <img src=x onerror="steal()"> and Team'
        full = '<a href="https://source.example/work">Required credit</a><script>steal()</script><img src=x onerror="steal()">' + CHANGE_NOTICE
        caption, description = build_public_attribution(values, full)
        view = Caption(caption)
        self.assertEqual({tag for tag, _attrs in view.tags}, {"a"})
        self.assertNotIn("steal()", view.text)
        self.assertIn("Paul", view.text)
        self.assertEqual(Caption(description).tags, [])
        self.assertEqual(html.unescape(description), html.unescape(full))
        self.assertTrue(all(not any(name.startswith("on") for name in attrs) for _tag, attrs in view.tags))

    def test_encoded_markup_is_not_reactivated_and_commons_text_is_not_double_escaped(self):
        values = metadata()
        values["creator_display_name"] = "Paul &amp; Team"
        full = "Paul &amp; Team &lt;img src=x onerror=bad&gt; Source: https://source.example/work"
        caption, description = build_public_attribution(values, full)
        self.assertIn("Paul &amp; Team", caption)
        self.assertNotIn("&amp;amp;", description)
        self.assertEqual(Caption(description).tags, [])
        self.assertEqual(html.unescape(description), html.unescape(full))

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
        self.assertIn("Archive Museum", Caption(payload["caption"]).text)
        self.assertIn(TITLE, Caption(payload["caption"]).text)
        self.assertIn(source, Caption(payload["caption"]).links)
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
                self.assertIn(name.upper(), Caption(caption).text)
                self.assertIn(url, Caption(caption).links)
                self.assertEqual(description, "Full public-domain provider audit")

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
                view = Caption(payload["caption"])
                self.assertIn(SOURCE, view.links)
                self.assertIn(url, view.links)
                self.assertIn(name.upper(), view.text)
                self.assertNotIn("Long optional", view.text)
                self.assertEqual(result["provider_attribution_text"], full)
                self.assertEqual(result["attribution_text"], full)
                self.assertEqual(html.unescape(payload["description"]), html.unescape(full))
        values = metadata()
        values.update(license_name="cc0", license_url="https://creativecommons.org/publicdomain/zero/1.0/", work_title="Map at https://title.example/reference")
        caption, _description = build_public_attribution(values, "Complete public-domain audit")
        self.assertIn(SOURCE, Caption(caption).links)

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
