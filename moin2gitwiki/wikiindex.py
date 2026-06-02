from __future__ import annotations

import os
import re
from datetime import datetime
from datetime import timedelta
from enum import Enum
from enum import auto
from typing import List, Optional
from urllib.parse import unquote

import attr

from .pagetree import PageTree
from .users import Moin2GitUser


class MoinEditType(Enum):
    PAGE_ADD = auto()
    PAGE_UPD = auto()
    PAGE_REN = auto()
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
        attachment: attachment filename for ATT_ADD/ATT_DEL edits, empty otherwise
        comment: comment field — used as git commit message when non-empty
        user: the mapped moin user
        ctx: Context — used for moin_data path, logging, and config flags

    """

    edit_date: datetime = attr.ib()
    page_revision: str = attr.ib()
    edit_type: MoinEditType = attr.ib()
    page_name: str = attr.ib()
    previous_page_name: Optional[str] = attr.ib(default=None)
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
    tree: PageTree = attr.ib()
    ctx = attr.ib(repr=False)

    @classmethod
    def create_edit_entries(cls, tree: PageTree, ctx) -> MoinEditEntries:
        pages_dir = os.path.join(ctx.moin_data, "pages")
        pages = os.listdir(pages_dir)
        epoch = datetime(1970, 1, 1)

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
                    # noinspection SpellCheckingInspection
                    if edit_type in ("SAVENEW", "SAVE", "SAVE/REVERT"):
                        ed_type = MoinEditType.PAGE_ADD if edit_type == "SAVENEW" else MoinEditType.PAGE_UPD
                    elif edit_type == "ATTNEW":
                        ed_type = MoinEditType.ATT_ADD
                    elif edit_type == "ATTDEL":
                        ed_type = MoinEditType.ATT_DEL
                    else:
                        # unrecognized edit_type - just move on
                        ctx.logger.warning(f"Unrecognized edit type {edit_type} on page {page}")
                        continue

                page_name = edit_fields[3]
                attachment = unquote(edit_fields[7])
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

                entries.append(entry)
                previous_page_name = page_name

        ctx.logger.debug("Sorting edit entries")
        entries.sort(key=lambda x: x.edit_date)

        ctx.logger.debug("Building edit entries object")

        return cls(
            entries=entries,
            tree=tree,
            ctx=ctx,
        )

    def count(self) -> int:
        return len(self.entries)

# end
