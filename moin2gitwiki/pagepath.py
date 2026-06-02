from __future__ import annotations

import re
from typing import List, Optional

import attr

from .appcontext import get_context


@attr.s(auto_attribs=True, slots=True, frozen=True)
class PagePath:
    """MoinMoin page name parsed into (is_category, parts) components.

    Attributes:
        is_category: True if the page name starts with 'Category' followed
                     by a non-empty name.
        parts:       Sanitized, non-empty path components.
    """
    is_category: bool
    parts: List[str]

    @property
    def category_name(self) -> Optional[str]:
        """Return the full MoinMoin category page name e.g. 'CategoryFoo', or None."""
        if self.is_category:
            return "Category" + self.parts[0]

        return None

    @classmethod
    def from_moin_name(cls, thing: str, force_category: bool = False) -> PagePath:
        """Decode MoinMoin name and convert to a page path.

        Processing steps:
        1. Decode hex sequences e.g. (20)->space, (2f)->/
        2. Sanitize each path component (spaces, dots etc. based on wiki type)
        3. Return sanitized parts as a list (no joining — callers decide)

        Controlled by context flags:
        - ctx.spaces_to_hyphens: replace spaces with hyphens (default: True for gollum/gitea)
        - ctx.strip_dots: remove dots (default: True for otterwiki)
        - ctx.subpages_as_dirs: keep / path delimiter (default: True for otterwiki)
        - ctx.category_folders: use page Category as a folder (default: False)
        """

        # Replace characters unsafe in filenames, preserving path separators.
        unsafe_chars = {
            "\\": "_",
            "*": "_",
            "?": "_",
            '"': "_",
            "<": "_",
            ">": "_",
            "|": "_",
            "\0": "_",
        }

        ctx = get_context()
        if ctx.spaces_to_hyphens:
            unsafe_chars[" "] = "-"

        if ctx.strip_dots:
            unsafe_chars["."] = ""

        parts = cls.decode_moin_name(thing).split("/")

        sanitized = []
        for part in parts:
            part = part.strip()
            for char, replacement in unsafe_chars.items():
                part = part.replace(char, replacement)
            if part:  # skip empty components that may result from stripping
                sanitized.append(part)

        if not ctx.subpages_as_dirs:
            sanitized = ["_".join(sanitized)]

        is_category = force_category

        if ctx.category_folders and sanitized:
            category = cls.strip_prefix(sanitized[0], "Category")
            if category is not None:
                is_category = True
                # only replace the first part if category name is not empty
                if category:
                    sanitized[0] = category

        return cls(is_category, sanitized)

    @staticmethod
    def decode_moin_name(thing: str) -> str:
        """Decode MoinMoin hex encoded sequences e.g. (20) -> space, (2e20) -> '. ' """

        def decode_hex(m):
            hex_str = m.group(1)
            try:
                return bytes.fromhex(hex_str).decode("utf-8")
            except Exception:
                return m.group(0)

        return re.sub(r'\(([0-9a-fA-F]+)\)', decode_hex, thing)

    @staticmethod
    def strip_prefix(text: str, prefix: str) -> Optional[str]:
        if text.startswith(prefix):
            return text.removeprefix(prefix).strip()

        return None

    @classmethod
    def moin_name_to_link(cls, thing: str) -> str:
        return cls.decode_moin_name(thing)
