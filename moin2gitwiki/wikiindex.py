from __future__ import annotations

import os
import re
from datetime import datetime
from datetime import timedelta
from enum import Enum
from enum import auto
from typing import List

import attr

from .pagepath import PagePath
from .pagetree import PageTree
from .users import Moin2GitUser


class MoinEditType(Enum):
    NEW = auto()
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
        page_revision: The revision id of this revision - a string of a zero-padded number
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


@attr.s(kw_only=True, frozen=True, slots=True)
class MoinEditEntries:
    """
    A sorted collection of Moin revision entry objects
    """

    entries: List[MoinEditEntry] = attr.ib()
    link_table: dict[str, str] = attr.ib()
    attachment_link_table: dict[str, MoinEditEntry] = attr.ib()
    category_tree: PageTree = attr.ib()
    ctx = attr.ib(repr=False)

    @classmethod
    def create_edit_entries(cls, ctx) -> MoinEditEntries:
        pages_dir = os.path.join(ctx.moin_data, "pages")
        pages = os.listdir(pages_dir)
        epoch = datetime(1970, 1, 1)
        attachment_link_table = {}
        link_table = {}

        tree = PageTree()

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
            previous_page_name = None
            for edit_line in edit_log_data:
                if not re.match(r"\d{15}", edit_line):
                    # skip if it is not a valid edit entry
                    continue

                # extract the fields out the edit entry
                edit_fields = edit_line.rstrip("\n").split("\t")
                edit_date = epoch + timedelta(microseconds=int(edit_fields[0]))
                page_revision = edit_fields[1]
                edit_type = edit_fields[2]

                if edit_type == "SAVE/RENAME":
                    ed_type = MoinEditType.RENAME
                else:
                    previous_page_name = None
                    if edit_type in ("SAVENEW", "SAVE", "SAVE/REVERT"):
                        if ctx.moin_data.joinpath("pages", page, "revisions", page_revision).is_file():
                            ed_type = MoinEditType.NEW if edit_type == "SAVENEW" else MoinEditType.PAGE
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
                        # unrecognized edit_type - just move on
                        continue

                page_name = edit_fields[3]
                attachment = edit_fields[7]
                entry = MoinEditEntry(
                    edit_date=edit_date,
                    page_revision=page_revision,
                    edit_type=ed_type,
                    page_name=page_name,
                    previous_page_name=previous_page_name,
                    attachment=attachment,
                    comment=edit_fields[8],
                    page_path=page,
                    user=ctx.users.get_user_by_id_or_anonymous(edit_fields[6]),
                    ctx=ctx,
                )
                entries.append(entry)

                # TODO: Eliminate?
                key = PagePath.moin_name_to_link(entry.page_name)
                link_table[key] = page_name

                # TODO: Eliminate?
                if ed_type == MoinEditType.ATTACH:
                    # use current page path (MoinMoin shows old revisions under current page name)
                    # if same name attachment was modified multiple times only most recent addition will be captured
                    # key = "\t".join([PagePath.moin_name_to_link(page_name), attachment])
                    key = "\t".join([PagePath.moin_name_to_link(page), attachment])
                    attachment_link_table[key] = entry

                # TODO: Handle attachment deletions

                previous_page_name = page_name

        ctx.logger.debug("Sorting edit entries")
        entries.sort(key=lambda x: x.edit_date)

        ctx.logger.debug("Building edit entries object")

        return cls(
            entries=entries,
            link_table=link_table,
            attachment_link_table=attachment_link_table,
            category_tree=tree,
            ctx=ctx,
        )

    def count(self) -> int:
        return len(self.entries)

    # FIXME
    def get_new_link_target(self, link):
        # FIXME: Eliminate link_table
        page_name = self.link_table.get(link)
        if page_name:
            # FIXME
            return self.category_tree.markdown_page_name(page_name)

        # FIXME:
        print(f"WARNING: No link map for {link}")

        return None

    # FIXME
    def get_new_attachment_link_target(self, link, attachment):
        key = "\t".join([link, attachment])
        revision = self.attachment_link_table.get(key)
        if revision:
            destination = self.category_tree.attachment_destination(revision.page_name, revision.attachment)
            if destination:
                self.ctx.logger.debug(f"Attachment: {link} {attachment} -> {destination}")
                return destination

        self.ctx.logger.debug(f"Attachment: no map for {link} {attachment}")
        return None

# end
