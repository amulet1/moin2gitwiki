"""
categorytree.py - Incremental category tree for moin2gitwiki

Maintains the mapping from MoinMoin pages/categories to git paths,
updated incrementally as revisions are processed in chronological order.

Path for any node:
    resolve(parent_category) / name

where resolve(parent_category) walks up the parent chain recursively.
category=None means the node lives at the root level.

Two traversal modes used by remove_node/delete_node and add_node:
  - _collect_delete_paths: leaves first, computes old paths from old prefix
  - _collect_add_paths:    parent first, computes new paths from new prefix

Callers are responsible for:
  - sanitizing names before passing them in
  - applying file extension to returned paths
  - emitting D commands from remove_node/delete_node results
  - emitting M commands from add_node results
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import NamedTuple, Optional


# ---------------------------------------------------------------------------
# Node key
# ---------------------------------------------------------------------------

class NodeKey(NamedTuple):
    """Composite key for the nodes dict — distinguishes categories from pages
    that share the same name or page_path string.
    """
    is_category: bool
    key: str


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """One page or category in the wiki tree.

    Attributes:
        is_category:  True if this is a CategoryFoo page.
        name:         Stripped category name for categories (e.g. "Foo"),
                      or sanitized page name for pages (e.g. "EMail").
        page_path:    MoinMoin filesystem page_path — stable unique key
                      for pages. None for category nodes.
        children:     Direct child nodes (both categories and pages).
        blob_mark:    Latest content mark — needed to re-emit the file
                      when the node moves.
        parent:       Direct reference to the parent Node, or None if root.
    """
    is_category: bool
    name: str
    page_path: Optional[str] = None
    children: list = field(default_factory=list)
    blob_mark: Optional[int] = None
    parent: Optional['Node'] = field(default=None, repr=False)


# Keep type aliases for clarity at call sites
CategoryNode = Node
PageNode = Node


# ---------------------------------------------------------------------------
# CategoryTree
# ---------------------------------------------------------------------------

class CategoryTree:
    """Incremental category tree mapping MoinMoin pages to output paths.

    Caller processes revisions in chronological order and calls:

        add_node(is_category, key, name, parent_category, blob_mark)
            -- when a page or category revision is processed.
            Returns list of (path, blob_mark) for M commands.

        remove_node(is_category, key)
            -- before add_node when a node is moving to a new location.
            Soft remove: keeps node in dict with children intact for re-add.
            Returns list of (path, blob_mark) for D commands.

        delete_node(is_category, key)
            -- when a node is actually deleted or renamed away.
            Hard remove: detaches children, removes from dict.
            Returns list of (path, blob_mark) for D commands.

    All returned paths have no file extension — callers add one if needed.
    """

    def __init__(self, logger: logging.Logger):
        self.nodes: dict[NodeKey, Node] = {}
        self.logger = logger

    # ------------------------------------------------------------------
    # Path computation
    # ------------------------------------------------------------------

    def _node_path(self, node: Node) -> str:
        """Compute the full path for a node by walking up the parent chain."""
        if node.parent is None:
            return node.name
        return "/".join(filter(None, (self._node_path(node.parent), node.name)))

    # ------------------------------------------------------------------
    # Tree structure helpers
    # ------------------------------------------------------------------

    def _get_or_create_category_node(self, name: str) -> Node:
        """Return the category node for name, creating a placeholder if needed."""
        key = NodeKey(True, name)
        if key not in self.nodes:
            self.nodes[key] = Node(is_category=True, name=name)
        return self.nodes[key]

    def _detach_from_parent(self, node: Node):
        """Remove node from its parent's children and clear parent pointer."""
        if node.parent is not None:
            if node in node.parent.children:
                node.parent.children.remove(node)
            node.parent = None

    def _attach_to_parent(self, node: Node, parent: Node):
        """Add node to parent's children and set parent pointer."""
        parent.children.append(node)
        node.parent = parent

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    def _collect_delete_paths(
        self, node: Node, prefix: str
    ) -> list[tuple[str, Optional[int]]]:
        """Collect (path, blob_mark) for subtree deletion, leaves first.

        prefix is the parent's path — passed down rather than walked up.
        """
        node_path = "/".join(filter(None, (prefix, node.name)))
        paths = []
        for child in node.children:
            paths.extend(self._collect_delete_paths(child, node_path))
        if node.blob_mark is not None:
            paths.append((node_path, node.blob_mark))
        return paths

    def _collect_add_paths(
        self, node: Node, prefix: str
    ) -> list[tuple[str, Optional[int]]]:
        """Collect (path, blob_mark) for subtree addition, parent first.

        prefix is the parent's path — passed down to children.
        Categories are processed before pages at each level.
        """
        node_path = "/".join(filter(None, (prefix, node.name)))
        if node.is_category:
            self.logger.debug("Category '%s' resolved -> '%s'", node.name, node_path)
        paths = [(node_path, node.blob_mark)]
        for child in node.children:
            if child.is_category:
                paths.extend(self._collect_add_paths(child, node_path))
        for child in node.children:
            if not child.is_category:
                paths.extend(self._collect_add_paths(child, node_path))
        return paths

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def placement_changed(
        self,
        is_category: bool,
        key: str,
        parent_category: Optional[str],
    ) -> bool:
        """Return True if the node exists and its parent would change.

        Used to decide whether to call remove_node before add_node.
        Returns False if the node doesn't exist yet.
        """
        node = self.nodes.get(NodeKey(is_category, key))
        if node is None:
            return False
        current_parent_name = node.parent.name if node.parent is not None else None
        return current_parent_name != parent_category

    def add_node(
        self,
        is_category: bool,
        key: str,
        name: str,
        parent_category: Optional[str],
        blob_mark: int,
    ) -> list[tuple[str, Optional[int]]]:
        """Add or update a node and return (path, blob_mark) for M commands.

        Finds an existing node (possibly soft-removed) or creates a new one.
        Attaches to parent, computes paths for the whole subtree.
        """
        node = self.nodes.get(NodeKey(is_category, key))
        if node is None:
            node = Node(
                is_category=is_category,
                name=name,
                page_path=None if is_category else key,
            )
            self.nodes[NodeKey(is_category, key)] = node

        node.blob_mark = blob_mark
        node.name = name

        new_parent = self._get_or_create_category_node(parent_category) if parent_category else None
        if node.parent is not None:
            self.logger.warning(
                "add_node called on already-attached node %r (parent=%r) — logic error or duplicate revision",
                key, node.parent.name,
            )
        elif new_parent is not None:
            self._attach_to_parent(node, new_parent)

        prefix = self._node_path(new_parent) if new_parent is not None else ""
        return self._collect_add_paths(node, prefix)

    def remove_node(
        self,
        is_category: bool,
        key: str,
    ) -> list[tuple[str, Optional[int]]]:
        """Soft-remove: detach from parent, keep in dict with children intact.

        Used before add_node when a node is moving to a new location.
        Returns (path, blob_mark) list for D commands, leaves first.
        """
        node = self.nodes.get(NodeKey(is_category, key))
        if node is None:
            self.logger.warning("remove_node called on unknown node %r", key)
            return []
        prefix = self._node_path(node.parent) if node.parent is not None else ""
        paths = self._collect_delete_paths(node, prefix)
        self._detach_from_parent(node)
        return paths

    def delete_node(
        self,
        is_category: bool,
        key: str,
    ) -> list[tuple[str, Optional[int]]]:
        """Hard-remove: detach, clear children's parents, remove from dict.

        Used for actual deletions and renames (old name removed).
        Returns (path, blob_mark) list for D commands, leaves first.
        """
        node = self.nodes.get(NodeKey(is_category, key))
        if node is None:
            self.logger.warning("delete_node called on unknown node %r", key)
            return []
        prefix = self._node_path(node.parent) if node.parent is not None else ""
        paths = self._collect_delete_paths(node, prefix)
        self._detach_from_parent(node)
        for child in node.children:
            child.parent = None
        del self.nodes[NodeKey(is_category, key)]
        return paths


    def all_paths(self) -> list[tuple[str, Optional[int]]]:
        """Return (path, blob_mark) for all non-placeholder nodes, root-down.

        Traverses from root nodes (parent=None) depth-first, passing the
        prefix down — O(n) without any parent chain walks.
        Categories are yielded before pages at each level.
        """
        roots = [n for n in self.nodes.values() if n.parent is None]
        result = []
        for root in sorted(roots, key=lambda n: n.name):
            result.extend(self._collect_add_paths(root, ""))
        return result

    def get_page_resolved(self, page_path: str) -> Optional[str]:
        """Return the current resolved path for a page, or None if not tracked."""
        node = self.nodes.get(NodeKey(False, page_path))
        return self._node_path(node) if node else None

    def get_category_resolved(self, name: str) -> Optional[str]:
        """Return the current resolved path for a category, or None if unknown."""
        node = self.nodes.get(NodeKey(True, name))
        return self._node_path(node) if node else None
