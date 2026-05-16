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
class CategoryNode:
    """One category in the hierarchy.

    Attributes:
        name:             Stripped category name e.g. "Foo" (from CategoryFoo)
        parent_category:  Stripped name of the parent category, or None
        suffix:           Path components between parent resolved path and this
                          node's name; comes from the /... part of the parent
                          ref in this category's page content.
                          e.g. "Sub" from [[CategoryFoo/Sub]]
        resolved:         Cached full folder path e.g. "Bar/Baz/Foo".
                          Always kept up to date; recomputed on any ancestor change.
        child_categories: Stripped names of direct child categories
                          (categories that declared this one as their parent).
        child_pages:      page_path keys of pages tagged with this category.
    """
    name: str
    parent_category: Optional[str] = None
    suffix: str = ""
    resolved: str = ""
    child_categories: set = field(default_factory=set)
    child_pages: set = field(default_factory=set)


@dataclass
class PageNode:
    """One wiki page in the tree.

    Attributes:
        page_path:        MoinMoin filesystem page_path — unique stable key.
        page_name:        Sanitized leaf name (may contain / for subpages).
                          Changes only on MoinMoin page rename.
        parent_category:  Stripped name of the category this page belongs to,
                          or None if the page has no category tag.
        suffix:           Path components from the category ref after /,
                          e.g. "Sub" from CategoryFoo/Sub.
                          Changes when the category ref in page content changes.
        resolved:         Cached full path (no file extension).
        blob_mark:        Latest content mark — set by caller after writing content.
    """
    page_path: str
    page_name: str
    parent_category: Optional[str] = None
    suffix: str = ""
    resolved: str = ""
    blob_mark: Optional[int] = None


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
        self.category_nodes: dict[str, CategoryNode] = {}
        self.page_nodes: dict[str, PageNode] = {}
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

    def _compute_page_resolved(self, page: PageNode) -> str:
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
        if page.page_name:
            parts.append(page.page_name)
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
        if resolved and self._path_registry.get(resolved) == page_path:
            del self._path_registry[resolved]

    # ------------------------------------------------------------------
    # Internal cascade helpers
    # ------------------------------------------------------------------

    def _recompute_category(self, name: str) -> list[tuple[str, str]]:
        """Recompute .resolved for a category node and cascade to all descendants.

        Returns list of (old_resolved, new_resolved) for every page that moved.
        Category folder paths themselves are not included — only page paths are.
        """
        node = self.category_nodes.get(name)
        if node is None:
            return []

        new_resolved = self._compute_category_resolved(name)
        if new_resolved == node.resolved:
            return []  # nothing changed — prune cascade early

        node.resolved = new_resolved
        self.logger.debug(f"Category '{name}' resolved -> '{new_resolved}'")

        renames: list[tuple[str, str]] = []

        # cascade to child categories first (depth-first so their .resolved
        # is correct before child pages are processed)
        for child_name in node.child_categories:
            renames.extend(self._recompute_category(child_name))

        # cascade to pages directly attached to this category
        for page_path in node.child_pages:
            page = self.page_nodes.get(page_path)
            if page:
                renames.extend(self._recompute_page(page))

        return renames

    def _recompute_page(self, page: PageNode) -> list[tuple[str, str]]:
        """Recompute .resolved for a page. Returns [(old, new)] if path changed."""
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
        return [(old_resolved, new_resolved)]

    def _detach_page_from_category(self, page: PageNode):
        """Remove page from its current parent category's child_pages set."""
        if page.parent_category and page.parent_category in self.category_nodes:
            self.category_nodes[page.parent_category].child_pages.discard(
                page.page_path
            )

    def _attach_page_to_category(self, page: PageNode):
        """Add page to its new parent category's child_pages set."""
        if page.parent_category:
            if page.parent_category not in self.category_nodes:
                # create a placeholder node — will be properly populated when
                # that category page is processed
                self.category_nodes[page.parent_category] = CategoryNode(
                    name=page.parent_category,
                    resolved=page.parent_category,
                )
            self.category_nodes[page.parent_category].child_pages.add(page.page_path)

    def _detach_category_from_parent(self, node: CategoryNode):
        """Remove category from its current parent's child_categories set."""
        if node.parent_category and node.parent_category in self.category_nodes:
            self.category_nodes[node.parent_category].child_categories.discard(
                node.name
            )

    def _attach_category_to_parent(self, node: CategoryNode):
        """Add category to its new parent's child_categories set."""
        if node.parent_category:
            if node.parent_category not in self.category_nodes:
                self.category_nodes[node.parent_category] = CategoryNode(
                    name=node.parent_category,
                    resolved=node.parent_category,
                )
            self.category_nodes[node.parent_category].child_categories.add(node.name)

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
            List of (old_resolved, new_resolved) for pages that moved.
            Category folder paths themselves are not included — only pages.
        """
        node = self.category_nodes.get(name)

        if node is None:
            # brand-new category
            node = CategoryNode(name=name, resolved=name)
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

        Returns list of (old_resolved, new_resolved) for affected pages.
        """
        node = self.category_nodes.get(name)
        if node is None:
            return []

        # detach from parent
        self._detach_category_from_parent(node)

        # detach child categories — they now have no parent
        for child_name in list(node.child_categories):
            child = self.category_nodes.get(child_name)
            if child:
                child.parent_category = None

        # detach child pages — they now have no parent category
        for page_path in list(node.child_pages):
            page = self.page_nodes.get(page_path)
            if page:
                page.parent_category = None

        del self.category_nodes[name]

        # recompute everything that was under this node
        renames: list[tuple[str, str]] = []

        for child_name in node.child_categories:
            renames.extend(self._recompute_category(child_name))

        for page_path in node.child_pages:
            page = self.page_nodes.get(page_path)
            if page:
                renames.extend(self._recompute_page(page))

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
            page = PageNode(page_path=page_path, page_name=page_name)
            self.page_nodes[page_path] = page

        old_resolved = page.resolved or None

        # update category membership if it changed
        if page.parent_category != parent_category:
            self._detach_page_from_category(page)
            page.parent_category = parent_category
            self._attach_page_to_category(page)
        else:
            page.parent_category = parent_category

        page.page_name = page_name
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
