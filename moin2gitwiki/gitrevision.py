import typing
from datetime import datetime
from typing import List, Optional

import attr

from .categorytree import CategoryTree
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
    _category_tree: CategoryTree = attr.ib(default=None, init=False)
    _home_exists: bool = attr.ib(default=False, init=False)
    home_overwritten: bool = attr.ib(default=False, init=False)

    def __attrs_post_init__(self):
        self._category_tree = CategoryTree(logger=self.ctx.logger)
        self.ctx.category_tree = self._category_tree

    def add_wiki_revision(
        self,
        revision: MoinEditEntry,
        content: bytes,
        primary_category: Optional[str] = None,
    ):
        """
        Add a wiki revision as a git commit

        Parameters:
            revision:          A wiki revision object
            content:           The content of the wiki object, after translation, as bytes
            primary_category:  Primary category detected from HTML content, or None

        """
        category_folders = self.ctx.category_folders
        if category_folders:
            np = revision.name_placement()
            placement = revision.category_placement(np=np, primary_category=primary_category)
            prev_placement = revision.prev_category_placement()
        else:
            placement = revision.plain_placement()
            prev_placement = revision.prev_plain_placement()

        tree = self._category_tree
        file_ops: List[str] = []
        description: Optional[str] = None

        if revision.edit_type == MoinEditType.ATTACH:
            blob_ref = self.output_blob(revision.attachment_content_bytes())
            dest = revision.attachment_destination()
            file_ops.append(f"M 100644 :{blob_ref} {dest}\n")
            description = f"Attach {revision.attachment} to {placement.page_name}"

        elif revision.edit_type == MoinEditType.DELETE:
            file_ops.extend(self._delete_side(revision, placement, tree))
            description = f"Delete {placement.category_name or placement.page_name}"

        elif revision.edit_type == MoinEditType.RENAME:
            if content is None:
                return
            blob_ref = self.output_blob(content)
            if prev_placement.page_name or prev_placement.category_name:
                file_ops.extend(self._delete_side(revision, prev_placement, tree))
            file_ops.extend(self._add_side(placement, revision.page_path, blob_ref, tree))
            description = f"Rename to {placement.category_name or placement.page_name}"

        elif revision.edit_type == MoinEditType.PAGE:
            if content is None:
                return
            blob_ref = self.output_blob(content)
            is_cat = placement.kind == "category"
            key = placement.category_name if is_cat else revision.page_path
            if tree.placement_changed(is_cat, key, placement.parent_category):
                file_ops.extend(self._delete_side(revision, placement, tree, soft=True))
            file_ops.extend(self._add_side(placement, revision.page_path, blob_ref, tree))
            description = f"Add/Update {placement.category_name or placement.page_name}"

        if not file_ops:
            return

        # track if a real Home page exists in the wiki
        if placement.page_name == "Home" and placement.parent_category is None:
            self._home_exists = True

        # in incremental mode, update Home.md as part of this commit
        if self.home_page == "incremental" and revision.edit_type != MoinEditType.ATTACH:
            home_content = self._generate_home_content().encode("utf-8")
            home_blob = self.output_blob(home_content)
            file_ops.append(f"M 100644 :{home_blob} Home.md\n")
            if self._home_exists:
                self.home_overwritten = True

        self._emit_commit(revision, description, file_ops)

    def _generate_home_content(self) -> str:
        """Generate Home page content from current tree state."""
        tree = self._category_tree
        current_paths = sorted(
            path for path, blob_mark in tree.all_paths()
            if blob_mark is not None
        )
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

    def emit_home_page(self):
        """Emit a commit adding or updating Home.md from current tree state."""
        content = self._generate_home_content().encode("utf-8")
        blob_ref = self.output_blob(content)
        if self._home_exists:
            self.home_overwritten = True
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
        self._emit_commit(revision, "Update Home page", [f"M 100644 :{blob_ref} Home.md\n"])

    def _delete_side(
        self,
        revision: MoinEditEntry,
        placement,
        tree: CategoryTree,
        soft: bool = False,
    ) -> List[str]:
        """Compute file ops for removing a page or category from the tree.

        soft=True: remove_node (node stays in dict, children intact for re-add).
        soft=False: delete_node (node removed from dict, children detached).
        """
        is_cat = placement.kind == "category"
        key = placement.category_name if is_cat else revision.page_path
        paths = tree.remove_node(is_cat, key) if soft else tree.delete_node(is_cat, key)
        file_ops: List[str] = []
        for path, blob_mark in paths:
            if path:
                file_ops.append(f"D {path}.md\n")
        return file_ops

    def _add_side(
        self,
        placement,
        page_path: str,
        blob_ref: int,
        tree: CategoryTree,
    ) -> List[str]:
        """Compute file ops for adding a page or category to the tree."""
        is_cat = placement.kind == "category"
        key = placement.category_name if is_cat else page_path
        name = placement.category_name if is_cat else placement.page_name
        paths = tree.add_node(
            is_cat, key, name,
            placement.parent_category,
            blob_ref,
        )
        file_ops: List[str] = []
        for path, blob_mark in paths:
            if blob_mark is not None:
                file_ops.append(f"M 100644 :{blob_mark} {path}.md\n")
        return file_ops

    def _emit_commit(
        self,
        revision: MoinEditEntry,
        description: Optional[str],
        file_ops: List[str],
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
        for op in file_ops:
            self.write_string(op)
        self.write_string("\n")
        self.last_commit_mark = commit_ref
        self.ctx.logger.debug(f"Written commit {commit_ref}")

    def write_changer(self, what: str, revision: MoinEditEntry):
        """
        Add an author/committer entry with date

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
