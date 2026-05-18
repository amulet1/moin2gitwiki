"""
categorytree.py - Incremental category tree for moin2gitwiki

Maintains the mapping from MoinMoin pages/categories to git paths,
updated incrementally as revisions are processed in chronological order.

Every page and category node is described by three fields:

    parent_category : str | None  -- stripped category name e.g. "Foo", or None
    suffix          : str         -- path components from the category ref after /
                                     e.g. "Sub" from CategoryFoo/Sub
                                          "Sub/Child" from CategoryFoo/Sub/Child
    page_name       : str         -- sanitized MoinMoin page name (may contain /
                                     for subpages), e.g. "Some/Page-Name"

Resolved path is always:

    resolved(parent_category) / suffix / page_name

with empty components omitted.  Category nodes use the same formula with
their own stripped name as page_name.

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
        parent_category:  Stripped name of the parent category, or None.
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
                          parent_category. Used for future path traversal.
    """
    is_category: bool
    name: str
    page_path: Optional[str] = None
    parent_category: Optional[str] = None
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

    def _compute_category_resolved(self, name: str) -> str:
        """Recursively compute the full folder path for a category.

        Walks up the parent chain using live node data, so always reflects
        the current tree state.  Does not use cached .resolved values —
        those are set by _recompute_category after this is called.
        """
        node = self.category_nodes.get(name)
        if node is None:
            # unknown / placeholder — use bare name
            return name
        parts = []
        if node.parent_category:
            parent_resolved = self._compute_category_resolved(node.parent_category)
            if parent_resolved:
                parts.append(parent_resolved)
        if node.suffix:
            parts.append(node.suffix)
        parts.append(node.name)
        return "/".join(parts)

    def _compute_page_resolved(self, page: Node) -> str:
        """Compute the full path for a page (no file extension).

        Uses the cached .resolved on the parent CategoryNode, which is
        always up to date because category cascade runs before page cascade.
        """
        parts = []
        if page.parent_category:
            cat = self.category_nodes.get(page.parent_category)
            parent_resolved = cat.resolved if cat else page.parent_category
            if parent_resolved:
                parts.append(parent_resolved)
        if page.suffix:
            parts.append(page.suffix)
        if page.name:
            parts.append(page.name)
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

    def _recompute_category(self, name: str) -> list[tuple[str, str, Optional[int]]]:
        """Recompute .resolved for a category node and cascade to all descendants.

        Returns list of (old_resolved, new_resolved, blob_mark) for every page
        or category that moved.  The top-level category itself is not included —
        callers handle that directly.
        """
        node = self.category_nodes.get(name)
        if node is None:
            return []

        new_resolved = self._compute_category_resolved(name)
        if new_resolved == node.resolved:
            return []  # nothing changed — prune cascade early

        node.resolved = new_resolved
        self.logger.debug(f"Category '{name}' resolved -> '{new_resolved}'")

        renames: list[tuple[str, str, Optional[int]]] = []

        # cascade categories first (their .resolved must be correct before
        # pages are recomputed), then pages
        for child in node.children:
            if child.is_category:
                old_child_resolved = child.resolved
                child_renames = self._recompute_category(child.name)
                new_child_resolved = child.resolved
                if old_child_resolved != new_child_resolved:
                    renames.append((old_child_resolved, new_child_resolved, child.blob_mark))
                renames.extend(child_renames)

        for child in node.children:
            if not child.is_category:
                page = self.page_nodes.get(child.page_path)
                if page:
                    renames.extend(self._recompute_page(page))

        return renames

    def _recompute_page(self, page: Node) -> list[tuple[str, str, Optional[int]]]:
        """Recompute .resolved for a page. Returns [(old, new, blob_mark)] if path changed."""
        old_resolved = page.resolved
        candidate = self._compute_page_resolved(page)
        new_resolved = self._unique_path(candidate, page.page_path)

        if new_resolved == old_resolved:
            return []

        self._unregister(old_resolved, page.page_path)
        self._register(new_resolved, page.page_path)
        page.resolved = new_resolved

        self.logger.debug(
            f"Page '{page.page_path}': '{old_resolved}' -> '{new_resolved}'"
        )
        return [(old_resolved, new_resolved, page.blob_mark)]

    def _detach_page_from_category(self, page: Node):
        """Remove page from its current parent category's children set."""
        if page.parent_category and page.parent_category in self.category_nodes:
            parent_node = self.category_nodes[page.parent_category]
            if page in parent_node.children:
                parent_node.children.remove(page)
        page.parent = None

    def _attach_page_to_category(self, page: Node):
        """Add page to its new parent category's children set."""
        if page.parent_category:
            if page.parent_category not in self.category_nodes:
                # create a placeholder node — will be properly populated when
                # that category page is processed
                self.category_nodes[page.parent_category] = Node(
                    is_category=True,
                    name=page.parent_category,
                    resolved=page.parent_category,
                )
            cat = self.category_nodes[page.parent_category]
            cat.children.append(page)
            page.parent = cat

    def _detach_category_from_parent(self, node: Node):
        """Remove category from its current parent's children set."""
        if node.parent_category and node.parent_category in self.category_nodes:
            parent_node = self.category_nodes[node.parent_category]
            if node in parent_node.children:
                parent_node.children.remove(node)
        node.parent = None

    def _attach_category_to_parent(self, node: Node):
        """Add category to its new parent's children set."""
        if node.parent_category:
            if node.parent_category not in self.category_nodes:
                self.category_nodes[node.parent_category] = Node(
                    is_category=True,
                    name=node.parent_category,
                    resolved=node.parent_category,
                )
            parent = self.category_nodes[node.parent_category]
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
            # brand-new category
            node = Node(is_category=True, name=name, resolved=name)
            self.category_nodes[name] = node

        changed = (
            node.parent_category != parent_category
            or node.suffix != suffix
        )
        if not changed:
            return []

        # detach from old parent
        self._detach_category_from_parent(node)

        node.parent_category = parent_category
        node.suffix = suffix

        # attach to new parent
        self._attach_category_to_parent(node)

        # recompute this node and everything below it
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
        self._detach_category_from_parent(node)

        # save children before deleting — we'll iterate them after
        children = list(node.children)

        # detach children — they now have no parent
        for child in children:
            child.parent_category = None
            child.parent = None

        del self.category_nodes[name]

        # recompute everything that was under this node
        renames: list[tuple[str, str, Optional[int]]] = []

        for child in children:
            if child.is_category:
                old_child_resolved = child.resolved
                child_renames = self._recompute_category(child.name)
                new_child_resolved = child.resolved
                if old_child_resolved != new_child_resolved:
                    renames.append((old_child_resolved, new_child_resolved, child.blob_mark))
                renames.extend(child_renames)

        for child in children:
            if not child.is_category:
                renames.extend(self._recompute_page(child))

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
        if page.parent_category != parent_category:
            self._detach_page_from_category(page)
            page.parent_category = parent_category
            self._attach_page_to_category(page)
        else:
            page.parent_category = parent_category

        page.name = page_name
        page.suffix = suffix
        page.blob_mark = blob_mark

        candidate = self._compute_page_resolved(page)
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

        self._detach_page_from_category(page)
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
