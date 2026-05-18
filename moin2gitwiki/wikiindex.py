import os
import re
from datetime import datetime
from datetime import timedelta
from enum import auto
from enum import Enum
from typing import NamedTuple
from typing import Optional

import attr

from .users import Moin2GitUser


class CategoryPlacement(NamedTuple):
    """Classification of a MoinMoin page for category tree placement.

    Attributes:
        kind:            One of 'category', 'subpage', or 'page'.
        category_name:   Stripped category key e.g. "Foo". Only set for kind='category'.
        parent_category: Stripped key of the parent category, or None.
        suffix:          Path components from the category ref between parent and
                         page_name. Only set for kind='page'; always "" otherwise.
        page_name:       Sanitized output path for this page (may contain /).
                         Not meaningful for kind='category'.
    """
    kind: str
    category_name: Optional[str]
    parent_category: Optional[str]
    suffix: str
    page_name: str


class MoinEditType(Enum):
    PAGE = auto()
    ATTACH = auto()
    RENAME = auto()
    DELETE = auto()


@attr.s(kw_only=True, frozen=True, slots=True)
class MoinEditEntry:
    """
    Represents a Moin page revision

    There are multiple revisions per page.

    Attributes:
        edit_date: The date of the edit
        page_revision: The revision id of this revision - a string of a zero padded number
        edit_type: Moin edit type
        page_name: The name of the page from the index file
        previous_page_name: The name the page previously had if renamed
        page_path: The name on the filesystem of the page
        attachment: attachment field - not used
        comment: comment filed - only used for git comments
        user: the mapped moin user
        ctx: Context - there for moin_path and logging

    """

    edit_date: datetime = attr.ib()
    page_revision: str = attr.ib()
    edit_type: MoinEditType = attr.ib()
    page_name: str = attr.ib()
    previous_page_name: str = attr.ib(default=None)
    page_path: str = attr.ib()
    attachment: str = attr.ib(default=None)
    comment: str = attr.ib(default="")
    user: Moin2GitUser = attr.ib()
    ctx = attr.ib(repr=False)

    def wiki_content_path(self):
        """The file pathname of the revision file"""
        return self.ctx.moin_data.joinpath(
            "pages",
            self.page_path,
            "revisions",
            self.page_revision,
        )

    def attachment_content_path(self):
        """The file pathname of the attachment file"""
        if self.attachment is None:
            raise ValueError("No attachment path set")
        return self.ctx.moin_data.joinpath(
            "pages",
            self.page_path,
            "attachments",
            self.attachment,
        )

    def attachment_content_bytes(self):
        """The content of the attachment retrieved as a byte string"""
        data = self.attachment_content_path().read_bytes()
        return data

    def attachment_destination(self):
        """The new pathname of the attachment file.

        Layout is determined by ctx.subpages_as_dirs and ctx.attachment_dir:
        - subpages_as_dirs=True  (otterwiki): PageName/<attachment_dir>/filename
        - subpages_as_dirs=False (gollum):    <attachment_dir>/PageName/filename

        attachment_dir defaults to 'a' for otterwiki, '_attachments' for gollum/gitea."""
        if self.attachment is None:
            raise ValueError("No attachment path set")
        subpages_as_dirs = self.ctx.subpages_as_dirs
        attachment_dir = self.ctx.attachment_dir
        decoded_page = self.resolved_page_name()
        if subpages_as_dirs:
            return os.path.join(decoded_page, attachment_dir, self.attachment)
        else:
            return os.path.join(attachment_dir, decoded_page, self.attachment)

    def decode_moin_name(self, thing: str) -> str:
        """Decode MoinMoin hex encoded sequences e.g. (20) -> space, (2e20) -> '. ' """
        def decode_hex(m):
            hex_str = m.group(1)
            try:
                return bytes.fromhex(hex_str).decode("utf-8")
            except Exception:
                return m.group(0)
        return re.sub(r'\(([0-9a-fA-F]+)\)', decode_hex, thing)

    def sanitize_for_path(self, thing: str) -> str:
        """Replace characters unsafe in filenames, preserving path separators.

        Controlled by context flags:
        - ctx.spaces_to_hyphens: replace spaces with hyphens (default: True for gollum/gitea)
        - ctx.strip_dots: remove dots (default: True for otterwiki)
        """
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
        spaces_to_hyphens = self.ctx.spaces_to_hyphens
        strip_dots = self.ctx.strip_dots
        parts = thing.split("/")
        sanitized = []
        for part in parts:
            part = part.strip()
            for char, replacement in unsafe_chars.items():
                part = part.replace(char, replacement)
            if spaces_to_hyphens:
                part = part.replace(" ", "-")
            if strip_dots:
                part = part.replace(".", "")
            if part:  # skip empty components that may result from stripping
                sanitized.append(part)
        return "/".join(sanitized)

    def unescape(self, thing: str) -> str:
        """Decode MoinMoin name for use in URLs"""
        return self.decode_moin_name(thing)

    def unescape_path(self, thing: str) -> str:
        """Decode MoinMoin name and sanitize for use in filesystem/git paths"""
        return self.sanitize_for_path(self.decode_moin_name(thing))

    def page_name_unescaped(self) -> str:
        """Unescape the page name"""
        return self.unescape(self.page_name)

    def page_path_unescaped(self) -> str:
        """Unescape the page path"""
        return self.unescape(self.page_path)

    def plain_placement(self) -> CategoryPlacement:
        """Classify this page for plain (non-category-folders) mode.

        No content reading — page_name is derived from the page name alone
        using markdown_transform().  Kind is always 'page', parent always None.
        """
        return CategoryPlacement(
            kind="page",
            category_name=None,
            parent_category=None,
            suffix="",
            page_name=self.markdown_transform(self.page_name),
        )

    def prev_plain_placement(self) -> CategoryPlacement:
        """Classify the previous page name for plain mode RENAME delete-side.

        Returns a dummy placement with empty page_name if there is no previous name.
        """
        if not self.previous_page_name:
            return CategoryPlacement(
                kind="page", category_name=None,
                parent_category=None, suffix="", page_name="",
            )
        return CategoryPlacement(
            kind="page",
            category_name=None,
            parent_category=None,
            suffix="",
            page_name=self.markdown_transform(self.previous_page_name),
        )

    def _classify_name_only(self, decoded: str) -> CategoryPlacement:
        """Classify a decoded page name without reading content.

        Used by name_placement(), prev_category_placement(), and
        prev_plain_placement(). For 'category' and 'page' kinds the
        parent_category and suffix fields are left empty — callers that
        need them must supply primary_category separately.
        """
        if decoded.startswith("Category"):
            stripped = decoded[len("Category"):].strip()
            if stripped:
                if "/" not in stripped:
                    return CategoryPlacement(
                        kind="category",
                        category_name=stripped,
                        parent_category=None,
                        suffix="",
                        page_name="",
                    )
                else:
                    slash = stripped.index("/")
                    cat_name = stripped[:slash].strip()
                    remainder = stripped[slash + 1:]
                    return CategoryPlacement(
                        kind="subpage",
                        category_name=None,
                        parent_category=cat_name,
                        suffix="",
                        page_name=self.sanitize_for_path(remainder),
                    )
        return CategoryPlacement(
            kind="page",
            category_name=None,
            parent_category=None,
            suffix="",
            page_name=self.sanitize_for_path(decoded),
        )

    def prev_category_placement(self) -> CategoryPlacement:
        """Classify the previous page name for RENAME delete-side handling.

        Name-only classification — content is never read for the old side
        of a rename.  Returns a dummy 'page' placement with empty page_name
        if there is no previous name.
        """
        if not self.previous_page_name:
            return CategoryPlacement(
                kind="page", category_name=None,
                parent_category=None, suffix="", page_name="",
            )
        return self._classify_name_only(
            self.decode_moin_name(self.previous_page_name)
        )

    def name_placement(self) -> CategoryPlacement:
        """Classify this page by name alone — no content reading.

        Returns kind, category_name, and page_name. For 'category' and
        'page' kinds, parent_category and suffix are always empty — callers
        must supply primary_category separately to complete the placement.
        """
        return self._classify_name_only(self.decode_moin_name(self.page_name))

    def category_placement(self, np: CategoryPlacement, primary_category: Optional[str] = None) -> CategoryPlacement:
        """Complete category tree placement given name classification and detected category.

        Parameters:
            np:               Result of name_placement() for this revision.
            primary_category: Primary category detected from HTML content,
                              Category prefix already stripped, or None.
        """
        if np.kind == "subpage":
            return np

        if np.kind == "category":
            # skip self-references
            ref = primary_category
            if ref and ref.split("/", 1)[0] == np.category_name:
                ref = None
            if ref:
                parts = ref.split("/", 1)
                parent_category = parts[0].strip()
                suffix = parts[1].strip() if len(parts) > 1 else ""
            else:
                parent_category = None
                suffix = ""
            return CategoryPlacement(
                kind="category",
                category_name=np.category_name,
                parent_category=parent_category,
                suffix=suffix,
                page_name="",
            )

        # 'page'
        if primary_category:
            parts = primary_category.split("/", 1)
            parent_category = parts[0].strip()
            suffix = parts[1].strip() if len(parts) > 1 else ""
        else:
            parent_category = None
            suffix = ""
        return CategoryPlacement(
            kind="page",
            category_name=None,
            parent_category=parent_category,
            suffix=suffix,
            page_name=np.page_name,
        )

    def markdown_transform(self, thing: str) -> str:
        """Decode MoinMoin name and convert to a page path.

        Processing steps:
        1. Decode hex sequences e.g. (20)->space, (2f)->/
        2. Sanitize each path component (spaces, dots etc. based on wiki type)
        3. Join with / (subpages_as_dirs) or _ (gollum/gitea)
        """
        subpages_as_dirs = self.ctx.subpages_as_dirs
        decoded = self.decode_moin_name(thing)
        parts = [self.sanitize_for_path(p) for p in decoded.split("/") if p]
        return "/".join(parts) if subpages_as_dirs else "_".join(parts)

    def markdown_page_path(self):
        """Page path translated, with .md suffix"""
        return self.markdown_page_name() + ".md"

    def markdown_page_name(self):
        """Page name translated, using category-resolved path when available"""
        return self.resolved_page_name()

    def resolved_page_name(self) -> str:
        """Return the current resolved page name from the category tree if available,
        falling back to markdown_transform() if the page is not yet tracked
        (e.g. during translation before the revision has been committed to the tree).
        """
        tree = self.ctx.category_tree
        if tree is not None:
            resolved = tree.get_page_resolved(self.page_path)
            if resolved is not None:
                return resolved
            placement = self._classify_name_only(self.decode_moin_name(self.page_name))
            if placement.kind == "category":
                resolved = tree.get_category_resolved(placement.category_name)
                if resolved is not None:
                    return resolved
        return self.markdown_transform(self.page_name)


@attr.s(kw_only=True, frozen=True, slots=True)
class MoinEditEntries:
    """
    A sorted collection of Moin revision entry objects
    """

    entries: list = attr.ib()
    link_table: dict = attr.ib()
    attachment_link_table: dict = attr.ib()
    ctx = attr.ib(repr=False)

    @classmethod
    def create_edit_entries(cls, ctx):
        pages_dir = os.path.join(ctx.moin_data, "pages")
        pages = os.listdir(pages_dir)
        epoch = datetime(1970, 1, 1)
        attachment_link_table = {}
        entries = []
        for page in pages:
            ctx.logger.debug(f"Reading page {page}")
            edit_log_file = os.path.join(pages_dir, page, "edit-log")
            # read the edit-log file
            try:
                with open(edit_log_file) as f:
                    edit_log_data = f.readlines()
            except OSError:
                ctx.logger.warning(f"No edit-log for page {page}")
                continue
            # read the lines in the edit-log file
            for edit_line in edit_log_data:
                if not re.match(r"\d{15}", edit_line):  # check its an edit entry
                    continue
                # extract the fields out the edit entry
                edit_fields = edit_line.rstrip("\n").split("\t")
                edit_date = epoch + timedelta(microseconds=int(edit_fields[0]))
                page_revision = edit_fields[1]
                edit_type = edit_fields[2]
                if edit_type == "SAVE/RENAME":
                    previous_page_name = page_name
                    ed_type = MoinEditType.RENAME
                else:
                    previous_page_name = None
                    if edit_type in ("SAVENEW", "SAVE", "SAVE/REVERT"):
                        if ctx.moin_data.joinpath(
                            "pages",
                            page,
                            "revisions",
                            page_revision,
                        ).is_file():
                            ed_type = MoinEditType.PAGE
                        else:
                            ed_type = MoinEditType.DELETE
                    elif edit_type == "ATTNEW":
                        attachment_path = os.path.join(
                            pages_dir,
                            page,
                            "attachments",
                            edit_fields[7],
                        )
                        if os.path.isfile(attachment_path):
                            # attachment exists
                            ed_type = MoinEditType.ATTACH
                        else:
                            # cannot find attachment - ignore it and move on
                            continue
                    else:
                        # unrecognised edit_type - just move on
                        continue
                page_name = edit_fields[3]
                entry = MoinEditEntry(
                    edit_date=edit_date,
                    page_revision=page_revision,
                    edit_type=ed_type,
                    page_name=page_name,
                    previous_page_name=previous_page_name,
                    attachment=edit_fields[7],
                    comment=edit_fields[8],
                    page_path=page,
                    user=ctx.users.get_user_by_id_or_anonymous(edit_fields[6]),
                    ctx=ctx,
                )
                entries.append(entry)
                if ed_type == MoinEditType.ATTACH:
                    key = "\t".join([entry.page_name_unescaped(), edit_fields[7]])
                    attachment_link_table[key] = entry
        ctx.logger.debug("Sorting edit entries")
        entries.sort(key=lambda x: x.edit_date)
        link_table = {revision.page_name_unescaped(): revision for revision in entries}
        ctx.logger.debug("Building edit entries object")
        return cls(
            entries=entries,
            link_table=link_table,
            attachment_link_table=attachment_link_table,
            ctx=ctx,
        )

    def count(self) -> int:
        return len(self.entries)

    def get_new_link_target(self, link):
        if link in self.link_table:
            return self.link_table[link].markdown_page_name()
        else:
            return None

    def get_new_attachment_link_target(self, link, attachment):
        key = "\t".join([link, attachment])
        if key in self.attachment_link_table:
            destination = self.attachment_link_table[key].attachment_destination()
            self.ctx.logger.debug(f"Attachment {link} {attachment} -> {destination}")
            return destination
        else:
            self.ctx.logger.debug(f"Attachment no map for {link} {attachment}")
            return None


# end
