import os
import re
from datetime import datetime
from datetime import timedelta
from enum import auto
from enum import Enum
from typing import Tuple

import attr

from .users import Moin2GitUser

# Regex patterns for MoinMoin category references
# Matches [[CategoryXxx]], [[CategoryXxx|label]], [[CategoryXxx/Sub|label]]
_CATEGORY_BRACKETED = re.compile(r'\[\[Category([^\]|]+?)(?:\|[^\]]*)?\]\]')
# Matches bare CategoryXxx at start of line or after whitespace (not mid-word)
_CATEGORY_BARE = re.compile(r'(?:^|(?<=\s))Category([\w/]+)')
# Matches a line consisting entirely of category references, whitespace and separators
_CATEGORY_ONLY_LINE = re.compile(
    r'^[\s\-]*(?:(?:\[\[Category[^\]]+\]\]|Category[\w/]+)[\s\-]*)+$'
)


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

    def wiki_content_bytes(self):
        """The content of the wiki revision retrieved as a byte string"""
        lines = self.wiki_content()
        if lines is None:
            return lines
        else:
            lines.append("")
            return "\n".join(lines).encode("utf-8")

    def wiki_content(self):
        """The content of the wiki revision as an array of strings"""
        lines = []
        try:
            lines = self.wiki_content_path().read_text().splitlines(keepends=False)
        except OSError:
            lines = None
        return lines

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
        subpages_as_dirs = getattr(self.ctx, "subpages_as_dirs", False)
        attachment_dir = getattr(self.ctx, "attachment_dir", "_attachments")
        decoded_page = self.markdown_transform(self.page_path).replace(".md", "")
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
        spaces_to_hyphens = getattr(self.ctx, "spaces_to_hyphens", True)
        strip_dots = getattr(self.ctx, "strip_dots", False)
        parts = thing.split("/")
        sanitized = []
        for part in parts:
            for char, replacement in unsafe_chars.items():
                part = part.replace(char, replacement)
            if spaces_to_hyphens:
                part = part.replace(" ", "-")
            if strip_dots:
                part = part.replace(".", "")
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

    def extract_category_refs(self):
        """Extract all category references from page content.

        Finds both bracketed ([[CategoryXxx]], [[CategoryXxx|label]]) and
        bare (CategoryXxx on its own line) category references.
        Skips MoinMoin comment lines (starting with ##).
        Returns list of category names with Category prefix stripped.
        """
        lines = self.wiki_content()
        if not lines:
            return []
        refs = []
        seen = set()
        for line in lines:
            # skip MoinMoin comment lines
            if line.startswith("##"):
                continue
            for m in _CATEGORY_BRACKETED.finditer(line):
                name = m.group(1).strip()
                if name not in seen:
                    seen.add(name)
                    refs.append(name)
            # bare CategoryXxx only accepted on lines consisting entirely
            # of category references — not in the middle of prose
            if _CATEGORY_ONLY_LINE.match(line):
                for m in _CATEGORY_BARE.finditer(line):
                    name = m.group(1).strip()
                    if name not in seen:
                        seen.add(name)
                        refs.append(name)
        return refs

    def primary_category_ref(self):
        """Return the first category reference from page content, or None.

        The returned name has the Category prefix already stripped.
        """
        refs = self.extract_category_refs()
        return refs[0] if refs else None

    def markdown_transform(self, thing: str) -> str:
        """Decode MoinMoin name and convert to a Markdown page path.

        Processing steps in order:
        1. Decode hex sequences e.g. (20)->space, (2f)->/
        2. Split on / to get path parts
        3. If first part starts with Category and category_folders is enabled:
           - Strip Category prefix from first part
           - Split on first / to get category name and optional suffix
           - Resolve category name via ctx.resolved_categories
           - Reassemble: resolved_path / suffix / remaining parts
        4. If not a category page and category_folders enabled:
           - Look up primary category ref from page content
           - Prepend resolved category path to parts
        5. Sanitize each part (spaces, dots etc. based on wiki type)
        6. Join parts with / (subpages_as_dirs) or _ (gollum/gitea)
        """
        subpages_as_dirs = getattr(self.ctx, "subpages_as_dirs", False)
        category_folders = getattr(self.ctx, "category_folders", False)

        # step 1+2: decode and split
        decoded = self.decode_moin_name(thing)
        parts = decoded.split("/")

        # step 3+4: category folder resolution
        if category_folders:
            if parts[0].startswith("Category"):
                # category page: strip prefix, split into name + suffix
                stripped = parts[0][len("Category"):]
                cat_parts = stripped.split("/", 1)
                cat_name = cat_parts[0]
                cat_suffix = cat_parts[1] if len(cat_parts) > 1 else None
                # resolve category name to full path
                resolved = self.ctx.resolve_category(cat_name)
                resolved_parts = resolved.split("/")
                if cat_suffix:
                    resolved_parts.append(cat_suffix)
                # append any remaining subpage parts
                parts = resolved_parts + parts[1:]
            else:
                # regular page: prepend resolved category if tagged
                cat_ref = self.primary_category_ref()
                if cat_ref:
                    # strip Category prefix if present
                    if cat_ref.startswith("Category"):
                        cat_ref = cat_ref[len("Category"):]
                    # split ref into category name + optional suffix
                    cat_parts = cat_ref.split("/", 1)
                    cat_name = cat_parts[0]
                    cat_suffix = cat_parts[1] if len(cat_parts) > 1 else None
                    resolved = self.ctx.resolve_category(cat_name)
                    resolved_parts = resolved.split("/")
                    if cat_suffix:
                        resolved_parts.append(cat_suffix)
                    parts = resolved_parts + parts

        # step 5: sanitize each part
        parts = [self.sanitize_for_path(p) for p in parts if p]

        # step 6: join
        if subpages_as_dirs:
            return "/".join(parts)
        else:
            return "_".join(parts)

    def markdown_page_path(self):
        """Page path translated"""
        return self._registered_page_name() + ".md"

    def markdown_page_name(self):
        """Page name translated"""
        return self._registered_page_name()

    def _registered_page_name(self):
        """Return markdown_transform result, with collision detection via path_registry."""
        if not getattr(self.ctx, "category_folders", False):
            return self.markdown_transform(self.page_name)

        registry = self.ctx.path_registry
        my_key = self.page_path
        if my_key in registry:
            return registry[my_key]

        candidate = self.markdown_transform(self.page_name)

        # collision detection — find a unique path
        final = candidate
        suffix = 1
        existing = set(registry.values())
        while final in existing:
            suffix += 1
            final = candidate + "_" + str(suffix)
        if final != candidate:
            self.ctx.logger.warning(
                f"Path collision for {self.page_name}: {candidate} already taken, "
                f"using {final}"
            )

        registry[my_key] = final
        return final


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

    def build_category_map(self):
        """Build the category hierarchy map from root category pages (pass 1).

        Only processes root category pages — those whose name after stripping
        the 'Category' prefix contains no '/'. For example:
        - CategoryDocs -> key='Docs' (processed)
        - CategoryTopics  -> key='Topics'  (processed)
        - CategoryTopics/Sub -> skipped (subpage, not a root category)

        For each root category page, finds the primary category reference in
        the page content and splits it into parent + suffix:
        - [[CategoryTopics/Sub]] -> parent='Topics', suffix='Sub'
        - [[CategoryTeam]] -> parent='Team', suffix=''

        Stores result in ctx.category_map:
            stripped_name -> (parent_or_None, suffix_or_empty_string)

        Example:
            CategoryDocs content has [[CategoryTeam/Archive]]:
                'Docs': ('Team', 'Archive')
            CategoryTeam content has [[CategoryOrg]]:
                'Team': ('Org', '')

        Then calls resolve_category_map() to build ctx.resolved_categories.
        """
        category_map = {}
        seen_pages = {}

        # collect current revision of each root category page
        # MoinMoin stores the current revision id in a 'current' file
        pages_dir = str(self.ctx.moin_data.joinpath("pages"))
        for entry in self.entries:
            decoded = entry.decode_moin_name(entry.page_name)
            if not decoded.startswith("Category"):
                continue
            stripped = decoded[len("Category"):]
            # skip subpages e.g. CategoryTopics/Sub
            if "/" in stripped:
                continue
            if stripped in seen_pages:
                continue
            if entry.edit_type != MoinEditType.PAGE:
                continue
            # check this is the current revision via the 'current' file
            current_file = os.path.join(pages_dir, entry.page_path, "current")
            try:
                current_rev = open(current_file).read().strip()
            except OSError:
                current_rev = None
            if current_rev and entry.page_revision != current_rev:
                continue
            seen_pages[stripped] = entry

        # build map entries
        for stripped, entry in seen_pages.items():
            refs = entry.extract_category_refs()
            parent = None
            suffix = ""
            for ref in refs:
                # strip Category prefix if present
                if ref.startswith("Category"):
                    ref = ref[len("Category"):]
                # skip self-references
                if ref.split("/", 1)[0] == stripped:
                    continue
                # found a valid parent ref — split into parent and suffix
                parts = ref.split("/", 1)
                parent = parts[0]
                suffix = parts[1] if len(parts) > 1 else ""
                break
            category_map[stripped] = (parent, suffix)
            self.ctx.logger.debug(
                f"Category map: '{stripped}' -> parent={parent!r}, suffix={suffix!r}"
            )

        self.ctx.category_map = category_map
        self.ctx.logger.info(
            f"Built category map with {len(category_map)} entries"
        )
        self.resolve_category_map()

    def resolve_category_map(self):
        """Resolve all category names to full paths (still part of pass 1).

        Iteratively resolves ctx.category_map {name: (parent, suffix)}
        into ctx.resolved_categories {name: full_path}.

        Example with:
            'Docs': ('Team', 'Archive')
            'Team': ('Org', '')

        Resolves to:
            'Team':  'Org'
            'Docs':   'Org/Archive/Docs'

        Cycle detection: if a full pass produces no resolutions, remaining
        entries must be in a cycle and are resolved using their name as-is.
        """
        unresolved = dict(self.ctx.category_map)
        resolved = {}

        while unresolved:
            stuck = True
            for name, (parent, suffix) in list(unresolved.items()):
                if parent is None or parent not in unresolved:
                    # parent is resolved, doesn't exist, or there is no parent
                    parent_path = resolved.get(parent, parent) if parent else None
                    parts = [p for p in [parent_path, suffix] if p]
                    if name != suffix:
                        parts.append(name)
                    resolved[name] = "/".join(parts) if parts else name
                    del unresolved[name]
                    stuck = False

            if stuck:
                # cycle detected — resolve remaining as-is
                for name in list(unresolved.keys()):
                    self.ctx.logger.warning(
                        f"Circular category reference detected for '{name}', "
                        f"using name as-is"
                    )
                    resolved[name] = name
                break

        self.ctx.resolved_categories = resolved
        self.ctx.logger.debug(f"Resolved categories: {resolved}")

    def create_home_page(self) -> Tuple[MoinEditEntry, str]:
        """Builds a synthetic home page to link all the wiki entries together"""
        revision = MoinEditEntry(
            edit_date=datetime.now(),
            page_revision="1",
            edit_type=MoinEditType.PAGE,
            page_name="Home",
            attachment="",
            comment="Synthetic Home Page",
            page_path="Home",
            user=self.ctx.users.get_user_by_id_or_anonymous("0"),
            ctx=self.ctx,
        )
        pages = {}
        for entry in self.entries:
            page_path = entry.markdown_page_name()
            page_split = page_path.split("/")
            page_name = page_split.pop()
            pages[page_path] = (
                len(page_split) * "  "
            ) + f"- [{page_name}]({page_path})\n"
            while len(page_split) > 0:
                page_path = "/".join(page_split)
                page_name = page_split.pop()
                if page_path not in pages:
                    pages[page_path] = (len(page_split) * "  ") + f"- {page_name}\n"

        content = "# Home Page\n\n"
        for item in sorted(pages.keys()):
            content += pages[item]
        content += "\n----\n"

        return (revision, content)

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
