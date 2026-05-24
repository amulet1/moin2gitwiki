from __future__ import annotations

import os
import re
from datetime import datetime
from datetime import timedelta
from enum import Enum
from enum import auto
from typing import List
from urllib.parse import unquote

import attr

from .pagepath import PagePath
from .pagetree import PageTree
from .users import Moin2GitUser


class MoinEditType(Enum):
    PAGE_ADD = auto()
    PAGE_UPD = auto()
    PAGE_REN = auto()
    PAGE_DEL = auto()
    ATT_ADD = auto()
    ATT_DEL = auto()


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

    def content_path(self):
        """The file pathname of the revision file"""
        return self.ctx.moin_data.joinpath(
            "pages",
            self.page_path,
            "revisions",
            self.page_revision,
        )

    def attachment_path(self):
        """The file pathname of the attachment file"""
        if self.attachment is None:
            raise ValueError("No attachment path set")

        return self.ctx.moin_data.joinpath(
            "pages",
            self.page_path,
            "attachments",
            self.attachment,
        )


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
                    ed_type = MoinEditType.PAGE_REN
                else:
                    previous_page_name = None
                    if edit_type in ("SAVENEW", "SAVE", "SAVE/REVERT"):
                        if ctx.moin_data.joinpath("pages", page, "revisions", page_revision).is_file():
                            ed_type = MoinEditType.PAGE_ADD if edit_type == "SAVENEW" else MoinEditType.PAGE_UPD
                        else:
                            ed_type = MoinEditType.PAGE_DEL
                    elif edit_type == "ATTNEW":
                        ed_type = MoinEditType.ATT_ADD
                    elif edit_type == "ATTDEL":
                        ed_type = MoinEditType.ATT_DEL
                    else:
                        # unrecognized edit_type - just move on
                        print(f"WARNING: Unrecognized edit type {edit_type} on page {page}")
                        continue

                page_name = edit_fields[3]
                attachment = edit_fields[7]
                comment = edit_fields[8]

                entry = MoinEditEntry(
                    edit_date=edit_date,
                    page_revision=page_revision,
                    edit_type=ed_type,
                    page_name=page_name,
                    previous_page_name=previous_page_name,
                    attachment=attachment,
                    comment=comment,
                    page_path=page,
                    user=ctx.users.get_user_by_id_or_anonymous(edit_fields[6]),
                    ctx=ctx,
                )
                print(
                    f"DEBUG: {entry.edit_date} {entry.page_revision} T={entry.edit_type} P={entry.page_path} N={entry.page_name} A={entry.attachment}")

                entries.append(entry)
                previous_page_name = page_name

                # TODO: Eliminate?
                key = PagePath.moin_name_to_link(entry.page_name)
                link_table[key] = page_name

                # TODO: Eliminate?
                if ed_type == MoinEditType.ATT_ADD:
                    # use current page path (MoinMoin shows old revisions under current page name)
                    # if same name attachment was modified multiple times only most recent addition will be captured
                    # key = "\t".join([PagePath.moin_name_to_link(page_name), attachment])
                    if page != page_name:
                        print(
                            f"WARNING: Attachment {attachment} on page {page} is not under the same name as the page it was attached to {page_name}")

                    key = "\t".join([PagePath.moin_name_to_link(page), attachment])
                    attachment_link_table[key] = entry

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
        link = unquote(link)
        print(f"WARNING: get_new_link_target: {link}")

        page_name = self.link_table.get(link)
        if page_name:
            # FIXME
            return self.category_tree.markdown_page_name(page_name)

        # FIXME:
        print(f"WARNING: No link map for {link}")

        return None

    # FIXME
    def get_new_attachment_link_target(self, link: str, attachment: str):
        link = unquote(link)

        print(f"WARNING: get_new_attachment_link_target: {link} {attachment}")
        page = self.category_tree.lookup_page(link)

        destination_new = page.get_attachment_path(attachment) if page is not None else None

        key = "\t".join([link, attachment])
        revision = self.attachment_link_table.get(key)
        if revision:
            destination_new = page.get_attachment_path(attachment) if page is not None else None
            destination = self.category_tree.attachment_destination(revision.page_name, revision.attachment)
            if destination_new != destination:
                print(f"ATT WARNING: new={destination_new} old={destination}")

            if destination:
                self.ctx.logger.debug(f"Attachment: {link} {attachment} -> {destination}")
                return destination
        else:
            destination = None

        if destination_new != destination:
            print(f"ATT WARNING: new={destination_new} old={None}")

        self.ctx.logger.debug(f"Attachment: no map for {link} {attachment}")
        return None

# end
