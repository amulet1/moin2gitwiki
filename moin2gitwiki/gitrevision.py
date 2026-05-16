import typing
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
    _category_tree: Optional[CategoryTree] = attr.ib(default=None, init=False)

    def __attrs_post_init__(self):
        if getattr(self.ctx, "category_folders", False):
            self._category_tree = CategoryTree(logger=self.ctx.logger)
            self.ctx.category_tree = self._category_tree

    def add_wiki_revision(
        self,
        revision: MoinEditEntry,
        content: bytes,
    ):
        """
        Add a wiki revision as a git commit

        Parameters:
            revision:   A wiki revision object
            content:    The content of the wiki object, after translation, as bytes

        """
        if self._category_tree is not None:
            self._add_wiki_revision_categorized(revision, content)
        else:
            self._add_wiki_revision_plain(revision, content)

    def _add_wiki_revision_plain(
        self,
        revision: MoinEditEntry,
        content: bytes,
    ):
        """Revision handling without category tree — original behaviour."""
        name = revision.markdown_page_path()
        if content is not None:
            blob_ref = self.output_blob(content)
        elif revision.edit_type == MoinEditType.ATTACH:
            blob_ref = self.output_blob(revision.attachment_content_bytes())
        if self.last_commit_mark is None:
            self.write_string(f"reset {self.branch}\n")
        self.write_string(f"commit {self.branch}\n")
        commit_ref = self.write_next_mark()
        self.write_changer("author", revision)
        self.write_changer("committer", revision)
        if revision.comment != "":
            self.output_data_string(f"{revision.comment}\n")
        else:
            if revision.edit_type == MoinEditType.PAGE:
                self.output_data_string(f"Add/Update {name}\n")
            elif revision.edit_type == MoinEditType.RENAME:
                self.output_data_string(f"Rename to {name}\n")
            elif revision.edit_type == MoinEditType.DELETE:
                self.output_data_string(f"Delete {name}\n")
            elif revision.edit_type == MoinEditType.ATTACH:
                self.output_data_string(f"Attach {revision.attachment} to {name}\n")
        # commit mark
        if self.last_commit_mark is not None:
            self.write_string(f"from :{self.last_commit_mark}\n")
        # data change
        if revision.edit_type == MoinEditType.PAGE:
            self.write_string(f"M 100644 :{blob_ref} {name}\n\n")
        elif revision.edit_type == MoinEditType.RENAME:
            self.write_string(
                f"D {revision.markdown_transform(revision.previous_page_name)}\n",
            )
            self.write_string(f"M 100644 :{blob_ref} {name}\n\n")
        elif revision.edit_type == MoinEditType.DELETE:
            self.write_string(f"D {name}\n\n")
        elif revision.edit_type == MoinEditType.ATTACH:
            self.write_string(
                f"M 100644 :{blob_ref} {revision.attachment_destination()}\n\n",
            )
        self.last_commit_mark = commit_ref
        self.ctx.logger.debug(f"Written commit {commit_ref}")

    def _add_wiki_revision_categorized(
        self,
        revision: MoinEditEntry,
        content: bytes,
    ):
        """Revision handling using CategoryTree for path resolution.

        Computes file paths incrementally from the category tree rather than
        from the static pre-built category map.  A single commit may contain
        multiple file operations when a category change cascades to child pages
        or categories.
        """
        tree = self._category_tree
        placement = revision.category_placement()
        file_ops: List[str] = []  # file operation lines for this commit
        description: Optional[str] = None

        if revision.edit_type == MoinEditType.ATTACH:
            # Attachments are not affected by category placement
            blob_ref = self.output_blob(revision.attachment_content_bytes())
            dest = revision.attachment_destination()
            file_ops.append(f"M 100644 :{blob_ref} {dest}\n")
            description = f"Attach {revision.attachment} to {placement.page_name}"

        elif revision.edit_type == MoinEditType.DELETE:
            if placement.kind == "category":
                old_resolved = tree.get_category_resolved(placement.category_name)
                renames = tree.delete_category(placement.category_name)
                if old_resolved:
                    file_ops.append(f"D {old_resolved}.md\n")
                for old, new, blob_mark in renames:
                    if blob_mark is not None:
                        file_ops.append(f"D {old}.md\n")
                        file_ops.append(f"M 100644 :{blob_mark} {new}.md\n")
                description = f"Delete {placement.category_name}"
            else:
                old_resolved = tree.delete_page(revision.page_path)
                if old_resolved:
                    file_ops.append(f"D {old_resolved}.md\n")
                description = f"Delete {placement.page_name}"

        elif revision.edit_type in (MoinEditType.PAGE, MoinEditType.RENAME):
            if content is None:
                return  # nothing to commit
            blob_ref = self.output_blob(content)

            if placement.kind == "category":
                old_resolved = tree.get_category_resolved(placement.category_name)
                tree.set_category_blob_mark(placement.category_name, blob_ref)
                renames = tree.update_category(
                    placement.category_name,
                    placement.parent_category,
                    placement.suffix,
                )
                new_resolved = tree.get_category_resolved(placement.category_name)
                if old_resolved and old_resolved != new_resolved:
                    file_ops.append(f"D {old_resolved}.md\n")
                file_ops.append(f"M 100644 :{blob_ref} {new_resolved}.md\n")
                for old, new, blob_mark in renames:
                    if blob_mark is not None:
                        file_ops.append(f"D {old}.md\n")
                        file_ops.append(f"M 100644 :{blob_mark} {new}.md\n")
                description = f"Update {new_resolved}"

            else:  # 'page' or 'subpage'
                # For RENAME, page_path is stable (MoinMoin renames the directory);
                # update_page detects the path change from old vs new placement.
                old_resolved, new_resolved = tree.update_page(
                    revision.page_path,
                    placement.page_name,
                    placement.parent_category,
                    placement.suffix,
                    blob_ref,
                )
                if old_resolved:
                    file_ops.append(f"D {old_resolved}.md\n")
                file_ops.append(f"M 100644 :{blob_ref} {new_resolved}.md\n")
                description = f"Add/Update {new_resolved}"

        if not file_ops:
            return

        self._emit_commit(revision, description, file_ops)

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
