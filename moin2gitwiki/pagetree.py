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
from typing import Dict, Optional, List

import attr

from moin2gitwiki.appcontext import get_context
from moin2gitwiki.pagepath import PagePath

NO_BLOB = 0


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
    _parent: Optional[Node] = attr.ib(repr=False)
    _category: Optional[Node] = attr.ib(repr=False)
    attachments: Optional[Dict[str, int]]
    blob_mark: int
    children: Dict[str, Node]

    def __init__(self, name: str, parent: Optional[Node] = None):
        self.name = name
        self.blob_mark = NO_BLOB
        self.attachments = None
        self.children = {}
        self._parent = parent
        self._category = None

    @property
    def is_empty(self) -> bool:
        if self.blob_mark == NO_BLOB:
            assert self._category is None
            # assert self.attachments is None
            return True

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

    def get_attachment_path(self, attachment: str, path: str = "") -> str:
        if path == "":
            path = self.get_path()

        ctx = get_context()
        attachment_dir = ctx.attachment_dir

        if attachment_dir != "":
            if ctx.subpages_as_dirs:
                path += "/" + attachment_dir
            else:
                path = attachment_dir + "/" + path

        return path + "/" + attachment

    def get_path(self, use_category: bool = True) -> str:
        """Compute the full path for a node by walking up the parent chain."""

        node = self

        parts: list = []
        while node and node.name != "":
            parts.append(node.name)
            if use_category and node._category:
                node = node._category
            else:
                node = node._parent

        parts.reverse()
        return "/".join(parts)

    def get_attachment(self, attachment: str, remove: bool) -> int:
        if self.attachments:
            if remove:
                blob_mark = self.attachments.pop(attachment, None)
                if blob_mark is None:
                    blob_mark = NO_BLOB
                elif not self.attachments:
                    self.attachments = None
            else:
                blob_mark = self.attachments.get(attachment, NO_BLOB)
        else:
            blob_mark = NO_BLOB

        return blob_mark

    def add_attachment(self, file_ops: dict[str, int], attachment: str, blob_mark: int, path: str = ""):
        if self.attachments is None:
            self.attachments = {}

        self.attachments[attachment] = blob_mark
        if blob_mark != NO_BLOB:
            dest = self.get_attachment_path(attachment, path)
            file_ops[dest] = blob_mark

    def remove_attachment(self, file_ops: dict[str, int], attachment: str):
        blob_mark = self.get_attachment(attachment, True)
        if blob_mark == NO_BLOB:
            print(f"WARNING: update_attachments: attachment {attachment} not found on page {self.get_path()}")
        else:
            dest = self.get_attachment_path(attachment)
            file_ops[dest] = NO_BLOB

    def update(self, file_ops: dict[str, int], category: Optional[Node], blob_mark: int) -> Optional[dict[str, int]]:
        path_changed = category is not self._category

        print(f"update: {self.name} changed={path_changed}")

        self._collect_paths(False, path_changed, file_ops)

        self.blob_mark = blob_mark
        self.update_category(category)

        if blob_mark == NO_BLOB:
            attachments = self.attachments
            self.attachments = None
        else:
            attachments = None

        self._collect_paths(True, path_changed, file_ops)

        return attachments

    def update_category(self, category: Optional[Node]) -> bool:
        """Update the category reference, return True if changed."""
        if self._category is not category:
            # category changed
            # FIXME: Address possible name collisions (node can be in children due to category or parent or both!)
            if self._category:
                # remove node from old category
                print(f"Removing node {self.name} from category {self._category.name}")
                del self._category.children[self.name]

            if category is not None:
                # check for collisions
                if category.children.get(self.name) is None:
                    print(f"Adding node {self.name} to category {category.name}")
                    category.children[self.name] = self
                else:
                    # collision
                    print(f"Warning: Collision in category tree: {self.name} already exists")
                    if self._category is None:
                        return False  # no change

                    category = None

            # update category reference
            self._category = category
            return True

        return False

    def delete_empty_leaves(self):
        # clean up the tree
        node = self
        while node.is_empty and node._parent and not node.children:
            # no children, we can delete the node
            print(f"Deleting node {node.name} with no children")
            parent = node._parent
            node._parent = None
            del parent.children[node.name]
            node = parent

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    def _collect_paths(self, add: bool, recurse: bool, paths: dict[str, int]):
        """Collect path and blob_mark for subtree addition."""
        stack: list[tuple[Node, str]] = [(self, self.get_path())]
        while stack:
            node, path = stack.pop()
            if node.blob_mark != NO_BLOB:
                paths[path + ".md"] = node.blob_mark if add else NO_BLOB

            if recurse:
                if node.attachments is not None:
                    for attachment, blob_mark in node.attachments.items():
                        if blob_mark != NO_BLOB:
                            paths[self.get_attachment_path(attachment, path)] = blob_mark if add else NO_BLOB

                for name, child in node.children.items():
                    stack.append((child, path + "/" + name))

    def collect_all_paths(self, paths: List[str], node_path: str):
        """Collect path for subtree addition, leaves last.

        """
        if not self.is_empty:
            paths.append(node_path)

        for name, node in self.children.items():
            category = node._category
            if category is None:
                path = node_path
            else:
                path = category.get_path()

            node.collect_all_paths(paths, path + "/" + name)

        return paths


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

        self.regular.collect_all_paths(paths, "")
        self.category.collect_all_paths(paths, "")

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
                print(f"Creating node {name} for {parent.get_path(False) if parent else '[root]'}")
                node = Node(name=name, parent=parent)
                parent.children[name] = node

        return node

    def add_page(
            self,
            file_ops: dict[str, int],
            new: bool,
            moin_page_name: str,
            moin_page_path: str,
            moin_category_name: Optional[str],
            blob_mark: int,
            old_attachments: Optional[dict[str, int]] = None
    ) -> Node:
        """Add or update a node and return (path, blob_mark) for M commands.

        Finds an existing node or creates a new one.
        Attaches to parent, computes paths for the whole subtree.
        """
        print(
            f"add_page: new={new} name={moin_page_name} category={moin_category_name} mark={blob_mark} page={moin_page_path}")

        page = self.moin_name_to_node(True, moin_page_name)
        assert page is not None

        if moin_category_name is None:
            category = None
        else:
            print(f"add_side: category={moin_category_name}")
            category = self.moin_name_to_node(True, moin_category_name, force_category=True)

        if page.is_empty:
            # page does not exist
            if not new:
                self.logger.warning("add_page: page expected to exist (name=%r)", moin_page_name)
                print(self)
        else:
            # existing page
            if new:
                self.logger.warning("add_page: page already exists (name=%r)", moin_page_name)
                print(self)

        page.update(file_ops, category, blob_mark)

        # move attachments from old page
        if old_attachments:
            if page.attachments is None:
                path = page.get_path()
                for attachment, b_mark in old_attachments.items():
                    page.add_attachment(file_ops, attachment, b_mark, path)
            else:
                print(f"ERROR: add_page: attachments already exist on page {page.get_path(False)}")

        # TODO: Make it part of moin_name_to_node
        # add mapping for links
        path = PagePath.moin_name_to_link(moin_page_path)
        print(f"add_page: map[{path}] to {page.get_path(False)}")
        self.page_map[path] = page

        return page

    def delete_page(
            self,
            file_ops: dict[str, int],
            moin_page_name: str,
            moin_page_path: Optional[str] = None
    ) -> Optional[dict[str, int]]:
        """Delete a node and return the deleted node."""
        page = self.moin_name_to_node(False, moin_page_name)
        if page is None or page.is_empty:
            self.logger.warning("delete_page: page does not exist (name=%r)", moin_page_name)
            attachments = None
        else:
            # mark node as deleted
            attachments = page.update(file_ops, None, NO_BLOB)

            # clean up
            page.delete_empty_leaves()

        if moin_page_path is not None:
            path = PagePath.moin_name_to_link(moin_page_path)
            # TODO: Warn if it does not exist
            self.page_map.pop(path, None)

        return attachments

    def add_attachment(self, file_ops: dict[str, int], moin_page_name: str, moin_page_path: str, attachment: str,
                       blob_mark: int):
        """Add an attachment to a node and return (path, blob_mark) for M commands.
        """
        page = self.moin_name_to_node(True, moin_page_name)
        assert page is not None

        page.add_attachment(file_ops, attachment, blob_mark)

        # TODO: Make it part of moin_name_to_node
        # add mapping for links
        path = PagePath.moin_name_to_link(moin_page_path)
        print(f"add_attachment: map[{path}] to {page.get_path(False)}")
        self.page_map[path] = page

    def remove_attachment(self, file_ops: dict[str, int], moin_page_name: str, attachment: str):
        """
        Removes an attachment from a specified MoinMoin page.
        """
        page = self.moin_name_to_node(False, moin_page_name)
        if page is None:
            self.logger.warning(f"remove_attachment: page does not exist (name={moin_page_name})")
        else:
            page.remove_attachment(file_ops, attachment)

    # FIXME
    def attachment_destination(self, moin_page_name: str, attachment: str) -> Optional[str]:
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

        return page.get_attachment_path(attachment)

    def get_new_link_target(self, link) -> Optional[str]:
        page = self.lookup_page(link)
        if page:
            # TODO: Create relative links
            return page.get_path()

        print(f"WARNING: get_new_link_target: no map for {link}")
        return None

    def get_new_attachment_link_target(self, link: str, attachment: str) -> Optional[str]:
        page = self.lookup_page(link)
        if page and page.get_attachment(attachment, False) != NO_BLOB:
            # TODO: Create relative links
            destination = page.get_attachment_path(attachment)
            self.logger.debug(f"Attachment: {link} {attachment} -> {destination}")
            return destination

        self.logger.debug(f"Attachment: no map for {link} {attachment}")
        return None
