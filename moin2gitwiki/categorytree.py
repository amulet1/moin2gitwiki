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
from typing import Optional


# ---------------------------------------------------------------------------
# Node types
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """One page or category in the wiki tree.

    Represents both regular pages (is_category=False) and category pages
    (is_category=True). Category nodes are keyed by stripped name in
    category_nodes; page nodes are keyed by page_path in page_nodes.

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
        self.category_nodes: dict[str, Node] = {}
        self.page_nodes: dict[str, Node] = {}
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

    def _recompute_category(self, name: str) -> list[tuple[str, str, Optional[int]]]:
        """Recompute .resolved for a category and cascade to all descendants.

        Returns renames for descendants only — the category itself is not
        included, as callers handle its own file move directly.
        """
        node = self.category_nodes.get(name)
        if node is None:
            return []

        new_resolved = self._compute_resolved(node)
        if new_resolved == node.resolved:
            return []  # nothing changed — prune cascade early

        node.resolved = new_resolved
        self.logger.debug(f"Category '{name}' resolved -> '{new_resolved}'")

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
        if name not in self.category_nodes:
            self.category_nodes[name] = Node(
                is_category=True,
                name=name,
                resolved=name,
            )
        return self.category_nodes[name]

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

    def update_category(
        self,
        name: str,
        parent_category: Optional[str],
        suffix: str,
    ) -> list[tuple[str, str]]:
        """Update or create a category node.

        Call when a category page revision is processed.

        Parameters:
            name:            Stripped category name e.g. "Foo"
            parent_category: Stripped name of its parent, or None
            suffix:          Path suffix between parent and this node

        Returns:
            List of (old_resolved, new_resolved, blob_mark) for pages and child
            categories that moved.  The category itself is not included — callers
            handle its own file move directly.
        """
        node = self.category_nodes.get(name)

        if node is None:
            node = Node(is_category=True, name=name, resolved=name)
            self.category_nodes[name] = node

        new_parent = self._get_or_create_category_node(parent_category) if parent_category else None

        if node.parent == new_parent and node.suffix == suffix:
            return []

        self._detach_from_parent(node)
        node.suffix = suffix
        if new_parent is not None:
            self._attach_to_parent(node, new_parent)

        return self._recompute_category(name)

    def delete_category(self, name: str) -> list[tuple[str, str]]:
        """Remove a category node (e.g. page deleted or renamed away).

        Detaches the node from its parent.  Direct child categories lose their
        parent reference and degrade to bare-name resolution.  Direct child
        pages lose their parent and move to their name-only path.

        Returns list of (old_resolved, new_resolved, blob_mark) for affected pages
        and child categories.
        """
        node = self.category_nodes.get(name)
        if node is None:
            return []

        # detach from parent
        self._detach_from_parent(node)

        # save children before deleting — we'll iterate them after
        children = list(node.children)

        # detach children — they now have no parent
        for child in children:
            child.parent = None

        del self.category_nodes[name]

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

    def update_page(
        self,
        page_path: str,
        page_name: str,
        parent_category: Optional[str],
        suffix: str,
        blob_mark: int,
    ) -> tuple[Optional[str], str]:
        """Update or create a page node.

        Call when a page revision is processed.

        Parameters:
            page_path:        MoinMoin filesystem page_path (stable unique key)
            page_name:        Sanitized page name, may include / for subpages
            parent_category:  Stripped category name from page content, or None
            suffix:           Suffix from category ref e.g. "Sub" from CategoryFoo/Sub
            blob_mark:        Content mark just written for this revision

        Returns:
            (old_resolved, new_resolved) where old_resolved is None if the page
            is new or did not move.  Caller should move the page if old_resolved
            is set.
        """
        page = self.page_nodes.get(page_path)

        if page is None:
            page = Node(is_category=False, name=page_name, page_path=page_path)
            self.page_nodes[page_path] = page

        old_resolved = page.resolved or None

        # update category membership if it changed
        new_parent = self._get_or_create_category_node(parent_category) if parent_category else None
        if page.parent != new_parent:
            self._detach_from_parent(page)
            if new_parent is not None:
                self._attach_to_parent(page, new_parent)

        page.name = page_name
        page.suffix = suffix
        page.blob_mark = blob_mark

        candidate = self._compute_resolved(page)
        new_resolved = self._unique_path(candidate, page_path)

        if old_resolved and old_resolved != new_resolved:
            self._unregister(old_resolved, page_path)

        self._register(new_resolved, page_path)
        page.resolved = new_resolved

        moved = old_resolved if (old_resolved and old_resolved != new_resolved) else None
        return (moved, new_resolved)

    def delete_page(self, page_path: str) -> Optional[str]:
        """Remove a page node.

        Call when a page deletion is processed.

        Returns the page's last resolved path, or None if the page was not tracked.
        """
        page = self.page_nodes.get(page_path)
        if page is None:
            return None

        self._detach_from_parent(page)
        self._unregister(page.resolved, page_path)
        del self.page_nodes[page_path]

        return page.resolved or None

    def get_page_resolved(self, page_path: str) -> Optional[str]:
        """Return the current resolved path for a page, or None if not tracked."""
        page = self.page_nodes.get(page_path)
        return page.resolved if page else None

    def get_category_resolved(self, name: str) -> Optional[str]:
        """Return the current resolved path for a category, or None if unknown."""
        node = self.category_nodes.get(name)
        return node.resolved if node else None

    def set_category_blob_mark(self, name: str, blob_mark: int):
        """Store the latest content mark for a category page.

        Called after writing the category page content, before update_category.
        The mark is used to re-emit the file if the category later moves due
        to a cascade from an ancestor change.
        """
        node = self.category_nodes.get(name)
        if node is None:
            node = Node(is_category=True, name=name, resolved=name)
            self.category_nodes[name] = node
        node.blob_mark = blob_mark
