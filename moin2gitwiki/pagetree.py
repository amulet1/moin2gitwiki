"""
pagetree.py - Incremental page tree for moin2gitwiki

Maintains the mapping from MoinMoin pages/categories to git paths,
updated incrementally as revisions are processed in chronological order.

Two traversal modes used by remove_node/delete_node and add_node:
  - _collect_delete_paths: leaves first, computes old paths from the old prefix
  - _collect_add_paths:    parent first, computes new paths from a new prefix

Callers are responsible for:
  - sanitizing names before passing them in
  - applying file extension to returned paths
"""

from __future__ import annotations

import logging
from typing import Optional, List

import attr

from moin2gitwiki.appcontext import get_context
from moin2gitwiki.pagepath import PagePath


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

@attr.s(auto_attribs=True, slots=True)
class Node:
    """One page or category in the wiki tree.

    Attributes:
        name:         Stripped category name for categories (e.g. "Foo"),
                      or sanitized page name for pages (e.g. "EMail").
#        page_path:    MoinMoin filesystem page_path — stable unique key
#                      for pages. None for category nodes.
        children:     Direct child nodes.
        blob_mark:    Latest content mark — needed to re-emit the file
                      when the node moves.
        parent:       Direct reference to the parent Node, or None if root.
        category:     Reference to the page category or None.
    """
    name: str
    #   # to be deleted
    #   page_path: Optional[str] = None

    children: dict[str, Node] = {}
    blob_mark: Optional[int] = None
    parent: Optional[Node] = attr.ib(default=None, repr=False)
    category: Optional[Node] = attr.ib(default=None, repr=False)

    @property
    def exists(self) -> bool:
        return self.blob_mark is not None

    # ------------------------------------------------------------------
    # Path computation
    # ------------------------------------------------------------------

    def get_path(self) -> str:
        """Compute the full path for a node by walking up the parent chain."""

        node = self

        parts: list = []
        while node:
            parts.append(node.name)
            if node.category:
                node = node.category
            else:
                node = node.parent

        parts.reverse()
        return "/".join(parts)

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    def _collect_delete_paths(self, paths: List[str], node_path: str):
        """Collect path for subtree deletion, leaves first.

        """
        for name, child in self.children.items():
            if child.category is None:
                print(f"{node_path}/{name}")
                child._collect_delete_paths(paths, node_path + "/" + name)

        if self.blob_mark is not None:
            paths.append(node_path)

    def _collect_add_paths(self, paths: List[tuple[str, int]], node_path: str):
        """Collect (path, blob_mark) for subtree addition, leaves last.

        """
        print(f"Collecting add paths for {node_path}{self.children}")
        print(f"Node: {self}")

        if self.blob_mark is not None:
            paths.append((node_path, self.blob_mark))

        for name, node in self.children.items():
            if node.category is None:
                print(f"{node_path}/{name}")
                if self is node:
                    print("Skipping self")
                    sys.exit(1)
                
                node._collect_add_paths(paths, node_path + "/" + name)

    def collect_all_paths(self, paths: List[str], node_path: str):
        """Collect path for subtree addition, leaves last.

        """
        if self.blob_mark is not None:
            paths.append(node_path)

        for name, node in self.children.items():
            category = node.category
            if category is None:
                path = node_path
            else:
                path = category.get_path()

            node.collect_all_paths(paths, path + "/" + name)

        return paths

    def add_add_ops(self, file_ops: List[str]):
        """Compute file ops for adding a node to the tree."""
        node_prefix = self.get_path()

        paths = []
        self._collect_add_paths(paths, node_prefix)

        for path, blob_mark in paths:
            file_ops.append(f"M 100644 :{blob_mark} {path}.md\n")

    def add_delete_ops(self, file_ops: List[str]):
        """Compute file ops for removing a node from the tree."""
        node_prefix = self.get_path()

        paths = []
        self._collect_delete_paths(paths, node_prefix)

        for path in paths:
            file_ops.append(f"D {path}.md\n")


# ---------------------------------------------------------------------------
# CategoryTree
# ---------------------------------------------------------------------------
@attr.s(auto_attribs=True, slots=True)
class PageTree:
    """Incremental category tree mapping MoinMoin pages to output paths.

    Caller processes revisions in chronological order and calls:

        add_node(is_category, key, name, category, blob_mark)
            -- when a page or category revision is processed.
            Returns a list of (path, blob_mark) for M commands.

        remove_node(is_category, key)
            -- before add_node when a node is moving to a new location.
            Soft remove: keeps the node in dict with children intact for re-add.
            Returns a list of (path, blob_mark) for D commands.

        delete_node(is_category, key)
            -- when a node is actually deleted or renamed away.
            Hard remove: detaches children, removes from dict.
            Returns a list of (path, blob_mark) for D commands.

    All returned paths have no file extension — callers add one if needed.
    """
    regular: dict[str, Node] = {}
    categories: dict[str, Node] = {}

    @property
    def logger(self) -> logging.Logger:
        return get_context().logger

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def all_paths(self) -> List[str]:
        """Return (path, blob_mark) for all non-placeholder nodes, root-down.

        Traverses from root nodes (parent=None) depth-first, passing the
        prefix down — O(n) without any parent chain walks.
        Categories are yielded before pages at each level.
        """
        paths: List[str] = []

        for name, node in self.regular.items():
            node.collect_all_paths(paths, "")

        for name, node in self.categories.items():
            node.collect_all_paths(paths, "")

        return paths

    def resolve_to_node(self, create: bool, moin_page_name: str) -> Optional[Node]:
        """Walk up the tree to the node for the path, creating missing nodes if requested.

        """
        path = PagePath.from_moin_name(moin_page_name)
        nodes = self.categories if path.is_category else self.regular

        node = None
        parent = None

        for part in path.parts:
            node = nodes.get(part)
            if node is None:
                if not create:
                    break

                # create new node
                node = Node(name=part, parent=parent, category=None)
                nodes[part] = node

            parent = node
            nodes = node.children

        return node

    def add_side(
            self,
            new: bool,
            moin_page_name: str,
            moin_category_name: Optional[str],
            blob_mark: int
    ) -> List[str]:
        """Add or update a node and return (path, blob_mark) for M commands.

        Finds an existing node or creates a new one.
        Attaches to parent, computes paths for the whole subtree.
        """
        node = self.resolve_to_node(True, moin_page_name)
        assert node is not None

        category = self.resolve_to_node(True, moin_category_name) if moin_category_name else None

        file_ops: List[str] = []

        if node.blob_mark is None:
            # page does not exist
            if not new:
                self.logger.warning("add_node: page expected to exist (name=%r)", moin_page_name)
        else:
            # existing page
            if new:
                self.logger.warning("add_node: page already exists (name=%r)", moin_page_name)

            if category is not node.category:
                node.add_delete_ops(file_ops)

        node.category = category
        node.blob_mark = blob_mark

        node.add_add_ops(file_ops)

        return file_ops

    def delete_side(self, moin_page_name: str) -> List[str]:
        node = self.resolve_to_node(False, moin_page_name)

        file_ops: List[str] = []

        if node is None:
            self.logger.warning("delete_node: page does not exist (name=%r)", moin_page_name)
        else:
            node.add_delete_ops(file_ops)
            # TODO: Actually delete!

        return file_ops

    # FIXME
    def attachment_destination(self, moin_page_name: str, attachment: str) -> Optional[str]:
        """The new pathname of the attachment file.

        Layout is determined by ctx.subpages_as_dirs and ctx.attachment_dir:
        - subpages_as_dirs=True  PageName/<attachment_dir>/filename
        - subpages_as_dirs=False <attachment_dir>/PageName/filename

        attachment_dir defaults to 'a' for otterwiki, '_attachments' for gollum/gitea.
        """
        if attachment is None:
            raise ValueError("No attachment path set")

        ctx = get_context()

        # TODO: Check if it starts with "/"
        attachment_dir = ctx.attachment_dir

        page = self.resolve_to_node(False, moin_page_name)
        if page is None:
            self.logger.warning("attachment_destination: no page node for page %r", moin_page_name)
            return None

        decoded_page = page.get_path()
        if ctx.subpages_as_dirs:
            return decoded_page + "/" + attachment_dir + "/" + attachment

        return attachment_dir + "/" + decoded_page + "/" + attachment

    # FIXME
    def markdown_page_name(self, moin_page_name: str) -> Optional[str]:
        """Page name translated, using a category-resolved path when available"""
        page = self.resolve_to_node(True, moin_page_name)
        if page is None:
            self.logger.warning("attachment_destination: no page node for page %r", moin_page_name)
            return None

        path = page.get_path()

        self.logger.warning("markdown_page_name: name=%r path=%r", moin_page_name, path)

        return path
