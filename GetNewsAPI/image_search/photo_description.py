'''Prepare provider-supplied photo descriptions for stock media alt text.'''

from __future__ import annotations

import re

from .context import _plain_text


_PLACEHOLDERS = frozenset({
    'image', 'stock image', 'photo', 'stock photo', 'photograph', 'picture',
    'untitled', 'untitled work', 'no description', 'no alt text', 'n/a', 'none', 'unknown',
})
_CAMERA_FILENAME = re.compile(
    r'(?:dscf?|img|pxl|sam|dji)[_ -]?\d+(?:[_ -]\d+)*(?:\.(?:jpe?g|png|heic|webp|tiff?))?',
    re.IGNORECASE,
)
_ATTRIBUTION = re.compile(
    r'\b(?:(?:image|photo(?:graph)?)\s+(?:by|courtesy of)\b'
    r'|(?:artist|attribution|copyright|credit|license|licence|source)\s*:'
    r'|creative commons\b|cc[- ]?0\b|cc[- ]by\b)|©',
    re.IGNORECASE,
)


def normalize_photo_description(value: object) -> str:
    '''Return visible description text with attribution and links excluded.'''
    if not isinstance(value, str):
        return ''
    try:
        description = _plain_text(value[:4096])
    except (AssertionError, ValueError):
        return ''
    description = _ATTRIBUTION.split(description, maxsplit=1)[0]
    description = ' '.join(description.split()).strip(' ,;:|–—')
    description = re.sub(r'\s+([,.;:!?])', r'\1', description)
    if (
        not any(character.isalpha() for character in description)
        or any(character in description for character in '<>')
        or description.casefold().rstrip('.!?') in _PLACEHOLDERS
        or _CAMERA_FILENAME.fullmatch(description)
    ):
        return ''
    return description


def photo_subject_from_tags(value: object) -> str:
    '''Pick one provider tag, preferring a subject phrase over a single word.'''
    if not isinstance(value, str):
        return ''
    first_tag = ''
    for tag in value[:4096].split(','):
        description = normalize_photo_description(tag)
        if ',' in description:
            continue
        if ' ' in description:
            return description
        first_tag = first_tag or description
    return first_tag
