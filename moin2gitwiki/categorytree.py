"""
categorytree.py - Incremental category tree for moin2gitwiki

Maintains the mapping from MoinMoin pages/categories to git paths,
updated incrementally as revisions are processed in chronological order.

Every node has a name, optional suffix, and a parent pointer.

Resolved path is always:

    parent.resolved / suffix / name

with empty components omitted. The parent pointer is a direct Node
reference set by attach/detach helpers. Parent category is passed by
name to update_category/update_page (external API) and converted to
a node reference internally.

Callers are responsible for:
  - sanitizing page_name before passing it in
  - applying any output-format suffix (e.g. file extension) to resolved paths
  - acting on the (old_path, new_path) pairs returned by update/delete methods
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
# Node types
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """One page or category in the wiki tree.

    Represents both regular pages (is_category=False) and category pages
    (is_category=True). Category nodes are keyed by stripped name in
    nodes dict keyed by NodeKey(is_category, key).

    Attributes:
        is_category:      True if this is a CategoryFoo page.
        name:             Stripped category name for categories (e.g. "Foo"),
                          or sanitized page name for pages (e.g. "EMail/Setup").
        page_path:        MoinMoin filesystem page_path — stable unique key
                          for pages. None for category nodes.
        suffix:           Path components from the category ref after /,
                          e.g. "Sub" from [[CategoryFoo/Sub]].
        resolved:         Cached full path (no file extension).
                          Always kept up to date; recomputed on any ancestor change.
        children:         Direct child nodes (both category and page nodes).
                          Categories are cascaded before pages to ensure their
                          resolved paths are correct before pages are recomputed.
        blob_mark:        Latest content mark — needed to re-emit the file
                          when the node moves due to a cascade.
        parent:           Direct reference to the parent Node, or None if root.
                          Set and cleared by attach/detach helpers alongside
                          Maintained alongside suffix by attach/detach helpers.
    """
    is_category: bool
    name: str
    page_path: Optional[str] = None
    suffix: str = ""
    resolved: str = ""
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

        update_category(name, parent_category, suffix)
            -- when a category page is saved/updated.
            Returns list of (old_resolved, new_resolved) for cascade moves.

        delete_category(name)
            -- when a category page is deleted or renamed (pass old name).
            Returns list of (old_resolved, new_resolved) for affected pages.

        update_page(page_path, page_name, parent_category, suffix, blob_mark)
            -- when a regular page is saved/updated.
            Returns (old_resolved_or_None, new_resolved).
            old_resolved is non-None when the page moved.

        delete_page(page_path)
            -- when a page is deleted.
            Returns the page's last resolved path, or None.

    All returned paths have no file extension — callers add one if needed.
    Collision detection adds _2, _3 ... to resolved paths as needed.
    """

    def __init__(self, logger: logging.Logger):
        # NodeKey(is_category, key) -> Node; key is stripped name for categories,
        # page_path for pages
        self.nodes: dict[NodeKey, Node] = {}
        # reverse map: resolved_path -> page_path, for collision detection
        self._path_registry: dict[str, str] = {}
        self.logger = logger

    # ------------------------------------------------------------------
    # Path computation (stateless helpers)
    # ------------------------------------------------------------------

    def _compute_resolved(self, node: Node) -> str:
        """Compute the full path for a node using its parent pointer.

        Uses parent.resolved (cached) as the prefix. This is safe because
        cascade is always top-down — a parent's resolved is always current
        by the time we compute a child's resolved.

        Placeholder nodes (created before their page is processed) have
        resolved set to their bare name, which is correct for root-level
        nodes and will be updated when their page is eventually processed.
        """
        parts = []
        if node.parent is not None and node.parent.resolved:
            parts.append(node.parent.resolved)
        if node.suffix:
            parts.append(node.suffix)
        if node.name:
            parts.append(node.name)
        return "/".join(parts)

    # ------------------------------------------------------------------
    # Collision detection
    # ------------------------------------------------------------------

    def _unique_path(self, candidate: str, page_path: str) -> str:
        """Return a path unique in the registry, adding _N suffix if needed.

        If the candidate is already registered to *this* page_path, it is
        returned as-is (idempotent update).
        """
        if self._path_registry.get(candidate) == page_path:
            return candidate
        if candidate not in self._path_registry:
            return candidate
        n = 2
        while True:
            attempt = f"{candidate}_{n}"
            if self._path_registry.get(attempt) == page_path:
                return attempt
            if attempt not in self._path_registry:
                return attempt
            n += 1

    def _register(self, resolved: str, page_path: str):
        self._path_registry[resolved] = page_path

    def _unregister(self, resolved: str, page_path: str):
        """Remove entry only if it belongs to this page (guards against stale removes)."""
        if resolved is not None and self._path_registry.get(resolved) == page_path:
            del self._path_registry[resolved]

    # ------------------------------------------------------------------
    # Internal cascade helpers
    # ------------------------------------------------------------------

    def _recompute_node(self, node: Node) -> list[tuple[str, str, Optional[int]]]:
        """Recompute .resolved for a node and cascade to all descendants.

        Handles both category nodes and page nodes. Pages additionally use
        collision detection and update the path registry.

        Returns list of (old_resolved, new_resolved, blob_mark) for this
        node and every descendant that moved, with categories cascaded
        before pages at each level.
        """
        old_resolved = node.resolved
        new_resolved = self._compute_resolved(node)

        if not node.is_category:
            new_resolved = self._unique_path(new_resolved, node.page_path)
            if old_resolved and old_resolved != new_resolved:
                self._unregister(old_resolved, node.page_path)
            self._register(new_resolved, node.page_path)

        renames = []
        if new_resolved != old_resolved:
            renames.append((old_resolved, new_resolved, node.blob_mark))
            node.resolved = new_resolved

        # cascade: categories first so their resolved is correct before pages
        for child in node.children:
            if child.is_category:
                renames.extend(self._recompute_node(child))
        for child in node.children:
            if not child.is_category:
                renames.extend(self._recompute_node(child))

        return renames

    def _cascade_children(self, node: Node) -> list[tuple[str, str, Optional[int]]]:
        """Cascade resolved path recomputation to all children of node.

        Processes category children before page children at each level
        so category resolved paths are correct before pages use them.
        """
        renames: list[tuple[str, str, Optional[int]]] = []
        for child in node.children:
            if child.is_category:
                renames.extend(self._recompute_node(child))
        for child in node.children:
            if not child.is_category:
                renames.extend(self._recompute_node(child))
        return renames

    def _get_or_create_category_node(self, name: str) -> Node:
        """Return the category node for name, creating a placeholder if needed."""
        if NodeKey(True, name) not in self.nodes:
            self.nodes[NodeKey(True, name)] = Node(
                is_category=True,
                name=name,
                resolved=name,
            )
        return self.nodes[NodeKey(True, name)]

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
    # Public API — category operations
    # ------------------------------------------------------------------

    def update_node(
        self,
        is_category: bool,
        key: str,
        name: str,
        parent_category: Optional[str],
        suffix: str,
        blob_mark: int,
    ) -> tuple[Optional[str], str, list]:
        """Update or create a node — category or page.

        Parameters:
            is_category:      True for category pages, False for regular pages.
            key:              Dict key — stripped name for categories,
                              page_path for pages.
            name:             Sanitized name — same as key for categories,
                              page name for pages.
            parent_category:  Stripped parent category name, or None.
            suffix:           Path components from category ref after /.
            blob_mark:        Content mark just written for this revision.

        Returns:
            (old_resolved, new_resolved, cascade_renames) where old_resolved
            is None if the node did not move, and cascade_renames lists
            (old, new, blob_mark) triples for children that moved.
        """
        node = self.nodes.get(NodeKey(is_category, key))

        if node is None:
            node = Node(
                is_category=is_category,
                name=name,
                page_path=None if is_category else key,
            )
            self.nodes[NodeKey(is_category, key)] = node

        # always update blob_mark — needed for future cascade re-emissions
        node.blob_mark = blob_mark

        new_parent = self._get_or_create_category_node(parent_category) if parent_category else None

        if node.parent != new_parent:
            self._detach_from_parent(node)
            if new_parent is not None:
                self._attach_to_parent(node, new_parent)

        node.name = name
        node.suffix = suffix

        old_resolved = node.resolved or None

        if is_category:
            new_resolved = self._compute_resolved(node)
            if new_resolved == old_resolved:
                return None, node.resolved, []
            self.logger.debug("Category '%s' resolved -> '%s'", name, new_resolved)
            node.resolved = new_resolved
        else:
            candidate = self._compute_resolved(node)
            new_resolved = self._unique_path(candidate, key)
            if old_resolved and old_resolved != new_resolved:
                self._unregister(old_resolved, key)
            self._register(new_resolved, key)
            node.resolved = new_resolved

        moved = old_resolved if (old_resolved and old_resolved != new_resolved) else None
        return moved, new_resolved, self._cascade_children(node)

    def delete_category(self, name: str) -> list[tuple[str, str]]:
        """Remove a category node (e.g. page deleted or renamed away).

        Detaches the node from its parent.  Direct child categories lose their
        parent reference and degrade to bare-name resolution.  Direct child
        pages lose their parent and move to their name-only path.

        Returns list of (old_resolved, new_resolved, blob_mark) for affected pages
        and child categories.
        """
        node = self.nodes.get(NodeKey(True, name))
        if node is None:
            return []

        # detach from parent
        self._detach_from_parent(node)

        # save children before deleting — we'll iterate them after
        children = list(node.children)

        # detach children — they now have no parent
        for child in children:
            child.parent = None

        del self.nodes[NodeKey(True, name)]

        # recompute everything that was under this node
        renames: list[tuple[str, str, Optional[int]]] = []

        for child in children:
            if child.is_category:
                renames.extend(self._recompute_node(child))
        for child in children:
            if not child.is_category:
                renames.extend(self._recompute_node(child))

        return renames

    # ------------------------------------------------------------------
    # Public API — page operations
    # ------------------------------------------------------------------

    def delete_page(self, page_path: str) -> Optional[str]:
        """Remove a page node.

        Call when a page deletion is processed.

        Returns the page's last resolved path, or None if the page was not tracked.
        """
        page = self.nodes.get(NodeKey(False, page_path))
        if page is None:
            return None

        self._detach_from_parent(page)
        self._unregister(page.resolved, page_path)
        del self.nodes[NodeKey(False, page_path)]

        return page.resolved or None

    def get_page_resolved(self, page_path: str) -> Optional[str]:
        """Return the current resolved path for a page, or None if not tracked."""
        page = self.nodes.get(NodeKey(False, page_path))
        return page.resolved if page else None

    def get_category_resolved(self, name: str) -> Optional[str]:
        """Return the current resolved path for a category, or None if unknown."""
        node = self.nodes.get(NodeKey(True, name))
        return node.resolved if node else None
