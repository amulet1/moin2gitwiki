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
import sys
from typing import Dict, Optional, List

import attr

from moin2gitwiki.appcontext import get_context
from moin2gitwiki.pagepath import PagePath


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

@attr.s(auto_attribs=True, slots=True, init=False)
class Node:
    """One page or category in the wiki tree.

    Attributes:
        name:         Stripped category name for categories (e.g. "Foo"),
                      or sanitized page name for pages (e.g. "EMail").
        children:     Direct child nodes.
        blob_mark:    Latest content mark — needed to re-emit the file
                      when the node moves.
        _parent:      Direct reference to the parent Node, or None if root.
        _category:    Reference to the page category or None.
    """

    name: str
    children: Dict[str, Node] = attr.ib(repr=False)
    _parent: Optional[Node] = attr.ib(repr=False)
    _category: Optional[Node] = attr.ib(repr=False)
    blob_mark: Optional[int] = None
    attachments: Optional[List[str]] = None

    def __init__(self, name: str, parent: Optional[Node] = None):
        self.name = name
        self.blob_mark = None
        self.attachments = None
        self.children = {}
        self._parent = parent
        self._category = None

    @property
    def empty(self) -> bool:
        if self.blob_mark is None:
            assert self._category is None
            # assert self.attachments is None

        return False

    def dump(self, indent: int = 0) -> List[str]:
        prefix = "  " * indent

        result = [
            f"{prefix}- {self.name}: {self.blob_mark}",
            f"{prefix}    n: {self.get_path(False)}",
            f"{prefix}    p: {self.get_path(True)}"
        ]
        if self._category:
            result.append(f"{prefix}    c: {self._category.get_path(False)}")

        for child in sorted(self.children.values(), key=lambda n: n.name):
            result.extend(child.dump(indent + 1))

        return result

    # ------------------------------------------------------------------
    # Path computation
    # ------------------------------------------------------------------

    def get_path(self, use_category: bool = True) -> str:
        """Compute the full path for a node by walking up the parent chain."""

        node = self

        parts: list = []
        while node and node.name:
            parts.append(node.name)
            if use_category and node._category:
                node = node._category
            else:
                node = node._parent

        parts.reverse()
        return "/".join(parts)

    def update(self, category: Optional[Node], blob_mark: Optional[int], file_ops: Optional[List[str]]):
        changed = category is not self._category or blob_mark is not self.blob_mark
        if changed:
            if file_ops is not None:
                self.delete_page_ops(file_ops)

        self.blob_mark = blob_mark
        self.update_category(category)

    def update_category(self, new_category: Optional[Node]) -> bool:
        """Update the category reference, return True if changed."""
        if self._category is not new_category:
            # category changed
            if self._category:
                # remove node from old category
                print(f"Removing node {self.name} from category {self._category.name}")
                del self._category.children[self.name]

            if new_category is not None:
                # check for collisions
                if new_category.children.get(self.name) is None:
                    print(f"Adding node {self.name} to category {new_category.name}")
                    new_category.children[self.name] = self
                else:
                    # collision
                    print(f"Warning: Collision in category tree: {self.name} already exists")
                    if self._category is None:
                        return False  # no change

                    new_category = None

            # update category reference
            self._category = new_category
            return True

        return False

    def erase(self):
        self.blob_mark = None
        self.update_category(None)

    def delete_empty_leaves(self):
        # clean up the tree
        node = self
        while node.empty and node._parent and not node.children:
            # no children, we can delete the node
            print(f"Deleting node {node.name} with no children")
            parent = node._parent
            node._parent = None
            del parent.children[node.name]
            node = parent

    def get_category(self):
        return self._category

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    def _collect_delete_paths(self, paths: List[str], node_path: str):
        """Collect path for subtree deletion, leaves first.

        """
        print(f"Collecting delete paths for {node_path}")
        print("\n".join(self.dump(1)))

        for name, node in self.children.items():
            if node._category is None:
                # FIXME: Remove print
                print(f"{node_path}/{name}")
                node._collect_delete_paths(paths, node_path + "/" + name)

        if self.blob_mark is not None:
            paths.append(node_path)

    def _collect_add_paths(self, paths: List[tuple[str, int]], node_path: str):
        """Collect (path, blob_mark) for subtree addition, leaves last.

        """
        print(f"Collecting add paths for {node_path}")
        print("\n".join(self.dump(1)))

        if self.blob_mark is not None:
            paths.append((node_path, self.blob_mark))

        for name, node in self.children.items():
            if node._category is None:
                # FIXME: Remove
                print(f"{node_path}/{name}")
                if self is node:
                    print("ERROR: Self-reference")
                    sys.exit(1)

                node._collect_add_paths(paths, node_path + "/" + name)

    def collect_all_paths(self, paths: List[str], node_path: str):
        """Collect path for subtree addition, leaves last.

        """
        if self.blob_mark is not None:
            paths.append(node_path)

        for name, node in self.children.items():
            category = node._category
            if category is None:
                path = node_path
            else:
                path = category.get_path()

            node.collect_all_paths(paths, path + "/" + name)

        return paths

    def add_page_ops(self, file_ops: List[str]):
        """Compute file ops for adding a node to the tree."""
        node_prefix = self.get_path()

        paths = []
        self._collect_add_paths(paths, node_prefix)

        for path, blob_mark in paths:
            file_ops.append(f"M 100644 :{blob_mark} {path}.md\n")

    def delete_page_ops(self, file_ops: List[str]):
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
    regular: Node = Node(name="")
    category: Node = Node(name="")
    page_map: Dict[str, Node] = attr.Factory(dict)

    @property
    def logger(self) -> logging.Logger:
        return get_context().logger

    def __str__(self) -> str:
        lines = []

        lines.append("Regular pages:")

        for node in sorted(self.regular.children.values(), key=lambda n: n.name):
            lines.extend(node.dump(1))

        lines.append("")
        lines.append("Categories:")

        for node in sorted(self.category.children.values(), key=lambda n: n.name):
            lines.extend(node.dump(1))

        return "\n".join(lines)

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

        for name, node in self.regular.children.items():
            node.collect_all_paths(paths, "")

        for name, node in self.category.children.items():
            node.collect_all_paths(paths, "")

        return paths

    def lookup_page(self, moin_page: str) -> Optional[Node]:
        return self.page_map.get(moin_page)

    def moin_name_to_node(self, create: bool, moin_name: str, force_category: bool = False) -> Optional[Node]:
        """Walk up the tree to the node for the path, creating missing nodes if requested.

        """
        path = PagePath.from_moin_name(moin_name, force_category)

        node = self.category if path.is_category else self.regular
        for name in path.parts:
            parent = node
            node = parent.children.get(name)
            if node is None:
                if not create:
                    break

                # create new node
                print(f"Creating missing node {name} for {parent.name if parent else '[root]'}")
                node = Node(name=name, parent=parent)
                parent.children[name] = node

        return node

    def add_page(
            self,
            file_ops: List[str],
            new: bool,
            moin_page_name: str,
            moin_category_name: Optional[str],
            blob_mark: int,
            old_page: Optional[Node] = None
    ) -> Node:
        """Add or update a node and return (path, blob_mark) for M commands.

        Finds an existing node or creates a new one.
        Attaches to parent, computes paths for the whole subtree.
        """
        print(f"add_page: new={new} page={moin_page_name} category={moin_category_name} mark={blob_mark}")

        node = self.moin_name_to_node(True, moin_page_name)
        assert node is not None

        if moin_category_name is None:
            category = None
        else:
            print(f"add_side: category={moin_category_name}")
            category = self.moin_name_to_node(True, moin_category_name, force_category=True)

        if node.blob_mark is None:
            # page does not exist
            if not new:
                self.logger.warning("add_node: page expected to exist (name=%r)", moin_page_name)
                print(self)
        else:
            # existing page
            if new:
                self.logger.warning("add_node: page already exists (name=%r)", moin_page_name)
                print(self)

        # FIXME: update() should do it instead
        if category is not node.get_category():
            # category changed, delete page it and uncategorized children
            node.delete_page_ops(file_ops)

        node.update(category, blob_mark, None)

        # FIXME: update() should do it instead
        node.add_page_ops(file_ops)

        return node

    def delete_page(self, file_ops: List[str], moin_page_name: str) -> Optional[Node]:
        """Delete a node and return the deleted node."""
        page = self.moin_name_to_node(False, moin_page_name)
        if page is None or page.empty:
            self.logger.warning("delete_page: page does not exist (name=%r)", moin_page_name)
        else:
            old_category = page.get_category()
            if old_category is not None:
                page.delete_page_ops(file_ops)

            # mark node as deleted
            page.erase()

            if old_category is not None:
                # readd children pages to new path
                page.add_page_ops(file_ops)

            # clean up
            page.delete_empty_leaves()

        return page

    # FIXME
    def attachment_destination(self, mode: int, moin_page_name: str, attachment: str) -> Optional[str]:
        """The new pathname of the attachment file.

        Layout is determined by ctx.subpages_as_dirs and ctx.attachment_dir:
        - subpages_as_dirs=True  PageName/<attachment_dir>/filename
        - subpages_as_dirs=False <attachment_dir>/PageName/filename
        - mode: -1=delete, 0=check, 1=add
        attachment_dir defaults to 'a' for otterwiki, '_attachments' for gollum/gitea.
        """
        if attachment == "":
            raise ValueError("No attachment path set")

        page = self.moin_name_to_node(False, moin_page_name)
        if page is None:
            self.logger.warning("attachment_destination: no page node for page %r", moin_page_name)
            # FIXME
            print(self)
            return None

        if mode == 1:
            if page.attachments is None:
                page.attachments = []
            page.attachments.append(attachment)
        elif mode == -1:
            if page.attachments is not None:
                page.attachments.remove(attachment)
                if not page.attachments:
                    page.attachments = None

        path = page.get_path()

        ctx = get_context()
        attachment_dir = ctx.attachment_dir

        # TODO: Check if it starts with "/"
        if ctx.subpages_as_dirs:
            path = path + "/" + attachment_dir
        else:
            path = attachment_dir + "/" + path

        return path + "/" + attachment

    # FIXME
    def markdown_page_name(self, moin_page_name: str) -> Optional[str]:
        """Page name translated, using a category-resolved path when available"""
        page = self.moin_name_to_node(True, moin_page_name)
        if page is None:
            self.logger.warning("attachment_destination: no page node for page %r", moin_page_name)
            return None

        path = page.get_path()

        ### self.logger.warning("markdown_page_name: name=%r path=%r", moin_page_name, path)

        return path
