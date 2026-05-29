import os
import typing
from datetime import datetime
from typing import Optional, Dict

import attr

from .pagetree import PageTree, NO_BLOB
from .wikiindex import MoinEditEntry
from .wikiindex import MoinEditType


@attr.s(kw_only=True, slots=True)
class GitExportStream:
    """
    Output a git fast-export formatted stream for each revision

    This object handles the state information to output the git commits for
    the Moin wiki revisions.

    Attributes:
        output:     The output file stream of git fast-export commands
        mark_number: The current git mark number
        last_commit_mark: The git mark number of the last commit
        ctx:        The context object - used for `logger` and `user` mapping

    """

    output: typing.BinaryIO = attr.ib()
    mark_number: int = attr.ib(default=1)
    last_commit_mark: int = attr.ib(default=None)
    branch: str = attr.ib(default="refs/heads/master")
    ctx = attr.ib(repr=False)
    home_page: str = attr.ib(default="end")
    _tree: PageTree = attr.ib()

    home_overwritten: bool = attr.ib(default=False, init=False)
    _home_check: bool = attr.ib(default=True, init=False)

    def add_wiki_revision(
            self,
            revision: MoinEditEntry,
            content: bytes,
            category: Optional[str] = None,
    ):
        """
        Add a wiki revision as a git commit

        Parameters:
            revision: A wiki revision object
            content:  The content of the wiki object, after translation, as bytes
            category: Primary category detected from HTML content, or None
        """
        tree = self._tree
        description: Optional[str]

        file_ops: Dict[str, int] = {}

        if revision.edit_type == MoinEditType.ATT_ADD:
            attachment_path = revision.attachment_path()
            if os.path.isfile(attachment_path):
                data = attachment_path.read_bytes()
                blob_ref = self.output_blob(data)
            else:
                blob_ref = NO_BLOB
            tree.add_attachment(file_ops, revision.page_name, revision.page_path, revision.attachment, blob_ref)
            description = f"Attach {revision.attachment} to {revision.page_name}"

        elif revision.edit_type == MoinEditType.ATT_DEL:
            tree.remove_attachment(file_ops, revision.page_name, revision.attachment)
            description = f"Detach {revision.attachment} from {revision.page_name}"

        elif revision.edit_type == MoinEditType.PAGE_DEL:
            tree.delete_page(file_ops, revision.page_name, revision.page_path)
            description = f"Delete {revision.page_name}"

        elif revision.edit_type == MoinEditType.PAGE_REN:
            if content is None:
                # TODO: warning? Or process as type=DELETE?
                return
            blob_ref = self.output_blob(content)

            if revision.previous_page_name is None:
                # TODO: Warning
                old_attachments = None
            else:
                old_attachments = tree.delete_page(file_ops, revision.previous_page_name)

            tree.add_page(file_ops, True, revision.page_name, revision.page_path, category, blob_ref, old_attachments)
            description = f"Rename {revision.previous_page_name} to {revision.page_name}"

        elif revision.edit_type == MoinEditType.PAGE_ADD:
            if content is None:
                return
            blob_ref = self.output_blob(content)
            tree.add_page(file_ops, True, revision.page_name, revision.page_path, category, blob_ref)
            description = f"Add {revision.page_name}"

        elif revision.edit_type == MoinEditType.PAGE_UPD:
            if content is None:
                return
            blob_ref = self.output_blob(content)
            tree.add_page(file_ops, False, revision.page_name, revision.page_path, category, blob_ref)
            description = f"Update {revision.page_name}"

        if not file_ops:
            return

        # in incremental mode, update Home.md as part of this commit
        if self.home_page == "incremental" and revision.edit_type != MoinEditType.ATT_ADD:
            home_content = self._generate_home_content().encode("utf-8")
            home_blob = self.output_blob(home_content)
            file_ops["Home.md"] = home_blob

        self._emit_commit(revision, description, file_ops)

    def _generate_home_content(self) -> str:
        """Generate Home page content from the current tree state."""
        tree = self._tree
        current_paths = sorted(tree.all_paths())
        pages = {}
        for page_path in current_paths:
            page_split = page_path.split("/")
            page_name = page_split.pop()
            pages[page_path] = (len(page_split) * "  ") + f"- [{page_name}]({page_path})\n"
            while len(page_split) > 0:
                page_path = "/".join(page_split)
                page_name = page_split.pop()
                if page_path not in pages:
                    pages[page_path] = (len(page_split) * "  ") + f"- {page_name}\n"
        content = "# Home Page\n\n"
        for item in sorted(pages.keys()):
            content += pages[item]
        content += "\n----\n"
        return content

    def prepare_home_page(self, file_ops: Dict[str, int]):
        page_name = "Home"

        page = self._tree.moin_name_to_node(True, page_name)
        assert page is not None

        # track if a real Home page exists in the wiki
        if self._home_check:
            self._home_check = False
            if not page.is_empty:
                self.home_overwritten = True

        # TODO: use add_side()
        # TODO: time for incremental Home should come from the current revision

        content = self._generate_home_content().encode("utf-8")
        blob_ref = self.output_blob(content)
        file_ops[page_name + ".md"] = blob_ref

    def emit_home_page(self):
        """Emit a commit adding or updating Home.md from the current tree state."""

        file_ops: Dict[str, int] = {}
        self.prepare_home_page(file_ops)

        revision = MoinEditEntry(
            edit_date=datetime.now(),
            page_revision="1",
            edit_type=MoinEditType.PAGE_UPD,
            page_name="Home",
            attachment="",
            comment="Synthetic Home Page",
            page_path="Home",
            user=self.ctx.users.get_user_by_id_or_anonymous("0"),
            ctx=self.ctx,
        )
        self._emit_commit(revision, "Update Home page", file_ops)

    def _emit_commit(
            self,
            revision: MoinEditEntry,
            description: Optional[str],
            file_ops: Dict[str, int],
    ):
        """Write a commit with the given file operations."""
        if self.last_commit_mark is None:
            self.write_string(f"reset {self.branch}\n")
        self.write_string(f"commit {self.branch}\n")
        commit_ref = self.write_next_mark()
        self.write_changer("author", revision)
        self.write_changer("committer", revision)

        if revision.comment:
            self.output_data_string(f"{revision.comment}\n")
        else:
            self.output_data_string(f"{description}\n")

        if self.last_commit_mark is not None:
            self.write_string(f"from :{self.last_commit_mark}\n")

        for path, blob_mark in file_ops.items():
            if blob_mark == NO_BLOB:
                op = f"D {path}"
            else:
                op = f"M 100644 :{blob_mark} {path}"

            self.ctx.logger.debug(f"op: {op}")
            self.write_string(op + "\n")

        self.write_string("\n")
        self.last_commit_mark = commit_ref
        self.ctx.logger.debug(f"Written commit {commit_ref}")

    def write_changer(self, what: str, revision: MoinEditEntry):
        """
        Add an author/committer entry with a date

        Parameters:
            what:       Normally either `committer` or `author`
            revision:   A wiki revision object

        """
        self.write_string(
            f"{what} {revision.user.moin_name} <{revision.user.email}> {int(revision.edit_date.timestamp())} +0000\n",
        )

    def get_next_mark(self):
        """
        Increment and return the mark number
        """
        mark = self.mark_number
        self.mark_number += 1
        return mark

    def write_next_mark(self):
        """
        Write out the next mark number
        """
        mark = self.get_next_mark()
        self.write_string(f"mark :{mark}\n")
        return mark

    def output_blob(self, content: bytes):
        """
        Output a blob object

        Parameters:
            content:    The content of the blob, as bytes

        """
        self.output.write(b"blob\n")
        blob_ref = self.write_next_mark()
        self.output_data(content)
        return blob_ref

    def output_data(self, content: bytes):
        """
        Output a set of data bytes

        Parameters:
            content:    The content of data, as bytes

        """
        self.write_string(f"data {len(content)}\n")
        self.output.write(content)

    def write_string(self, string: str):
        """
        Write a string out with utf-8 encoding into bytes
        """
        self.output.write(string.encode("utf-8"))

    def output_data_string(self, string: str):
        """
        Write a string out as a data object with utf-8 encoding into bytes
        """
        self.output_data(string.encode("utf-8"))

    def end_stream(self):
        """
        Write the end of stream information
        """
        self.write_string(f"reset {self.branch}\n")
        self.write_string(f"from :{self.last_commit_mark}\n")

# end
