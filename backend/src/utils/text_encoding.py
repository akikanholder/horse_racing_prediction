"""Decode HTML and JSON bytes to Unicode.

EUC-JP and cp932 pages become Unicode. JSON later writes UTF-8.
A caller-supplied encoding is used when those bytes decode with it.
When they do not, the charset on the response or in the document head is
used. A forced ``utf-8`` read of an EUC-JP page is not left as mojibake.
"""

from __future__ import annotations

import re

_CHARSET = re.compile(rb"""charset\s*=\s*["']?\s*([A-Za-z0-9._+-]+)""", re.IGNORECASE)
_ALIASES = {
    "euc-jp": "euc_jp",
    "eucjp": "euc_jp",
    "x-euc-jp": "euc_jp",
    "x-eucjp": "euc_jp",
    "shift_jis": "cp932",
    "shift-jis": "cp932",
    "sjis": "cp932",
    "windows-31j": "cp932",
    "ms932": "cp932",
    "cp932": "cp932",
    "utf8": "utf-8",
    "utf-8": "utf-8",
}


def normalize_charset(name: str | None) -> str | None:
    """Map a declared charset onto a codec Python can open. Unknown names stay."""
    if not name:
        return None
    token = name.strip().strip("\"'").lower()
    if not token:
        return None
    return _ALIASES.get(token, token)


def charset_from_content_type(content_type: str | None) -> str | None:
    """Charset parameter of a Content-Type header. Absent means null."""
    if not content_type:
        return None
    match = re.search(r"charset\s*=\s*([A-Za-z0-9._+-]+)", content_type, re.IGNORECASE)
    if match is None:
        return None
    return normalize_charset(match.group(1))


def charset_from_head(content: bytes) -> str | None:
    """Charset in the first part of a document. The declaration itself is ASCII."""
    match = _CHARSET.search(content[:8192])
    if match is None:
        return None
    try:
        token = match.group(1).decode("ascii")
    except UnicodeDecodeError:
        return None
    return normalize_charset(token)


def _decodes(content: bytes, encoding: str) -> bool:
    try:
        content.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return False
    return True


def decode_page_bytes(
    content: bytes,
    *,
    requested: str | None = None,
    content_type: str | None = None,
) -> str:
    """Decode ``content`` to Unicode.

    ``requested`` wins when it decodes the bytes. An EUC-JP or cp932 page
    that was requested as UTF-8 is decoded with the charset the page or the
    response declared, then with EUC-JP, then cp932. The last resort replaces
    undecodable bytes. Characters are not taken from another field.
    """
    declared = charset_from_content_type(content_type) or charset_from_head(content)
    asked = normalize_charset(requested)
    if asked and _decodes(content, asked):
        return content.decode(asked)

    candidates: list[str] = []

    def add(name: str | None) -> None:
        if name and name not in candidates:
            candidates.append(name)

    add(declared)
    add(asked)
    add("utf-8")
    add("euc_jp")
    add("cp932")
    for encoding in candidates:
        if _decodes(content, encoding):
            return content.decode(encoding)
    fallback = candidates[0] if candidates else "utf-8"
    return content.decode(fallback, errors="replace")
