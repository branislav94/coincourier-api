from __future__ import annotations

import re

from .models import ImageCandidate


PROVIDER_LICENSES = {
    "pexels": ("pexels-license", "https://www.pexels.com/license/"),
    "pixabay": ("pixabay-content-license", "https://pixabay.com/service/license-summary/"),
}


def normalize_license_name(value: str | None) -> str:
    value = re.sub(r"[_\s]+", "-", (value or "").strip().lower())
    value = value.replace("creative-commons-", "cc-")
    aliases = {
        "zero": "cc0",
        "public-domain": "pdm",
        "public-domain-mark": "pdm",
        "by": "cc-by",
        "by-sa": "cc-by-sa",
        "by-nc": "cc-by-nc",
        "by-nd": "cc-by-nd",
        "by-nc-sa": "cc-by-nc-sa",
        "by-nc-nd": "cc-by-nc-nd",
    }
    return aliases.get(value, value)


def _known_creator(value: str | None) -> bool:
    normalized = (value or "").strip().lower()
    return bool(normalized and normalized not in {"unknown", "unknown creator", "n/a", "none"})


class ImageLicensePolicy:
    def __init__(self, allowlist: tuple[str, ...] | list[str]) -> None:
        self.allowlist = {normalize_license_name(value) for value in allowlist}

    def validate(self, candidate: ImageCandidate) -> tuple[bool, str]:
        provider_license = PROVIDER_LICENSES.get(candidate.provider)
        if provider_license:
            expected_name, expected_url = provider_license
            if candidate.license_name == expected_name and candidate.license_url == expected_url:
                if candidate.attribution_text and candidate.source_page_url:
                    return True, ""
            return False, "license"

        license_name = normalize_license_name(candidate.license_name)
        if not license_name or license_name not in self.allowlist:
            return False, "license"
        if not candidate.source_page_url or not candidate.license_url or not candidate.attribution_text:
            return False, "license"
        if not candidate.metadata.get("attribution_complete"):
            return False, "license"
        if license_name == "cc-by":
            authoritative = bool(candidate.metadata.get("attribution_authoritative"))
            if not _known_creator(candidate.creator_name) and not authoritative:
                return False, "license"
        candidate.license_name = license_name
        return True, ""
