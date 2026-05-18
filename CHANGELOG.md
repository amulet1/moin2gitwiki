# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/en/1.0.0/)
and this project adheres to [Semantic Versioning](http://semver.org/spec/v2.0.0.html).

Unreleased Changes
------------------

### Fork changes

- fix: `KeyError` crash when processing `<a>` tags without `href` attribute
- fix: `KeyError` crash when processing `<img>` tags without `src` attribute
- fix: `ValueError` crash when `furl` misinterprets a relative wiki link
  as an invalid hostname during URL joining
- fix: content section lookup now supports both MoinMoin Explorer theme
  (`id="page_content"`) and standard Modern theme (`id="content"`)
- fix: universal MoinMoin hex encoding decoder in `wikiindex.py` replacing
  the `(2f)`-only `unescape()` — now decodes any hex sequence e.g.
  `(20)` → space, `(2d)` → `-`, `(2e20)` → `. `
- fix: attachment paths now decoded from MoinMoin encoding so they match
  decoded page names, preventing broken attachment links
- fix: paragraph breaks lost when stripping line marker CSS classes —
  `tag.unwrap()` replaced with `del tag["class"]` so pandoc preserves
  paragraph and list structure
- feat: add `--wiki-type [gollum|gitea|otterwiki]` option to `fast-export`
  (default: `gollum`) — sets appropriate defaults for page naming, subpage
  structure and attachment layout per target platform
- feat: add fine-grained conversion flags to `fast-export`, all defaulting
  to `None` and derived from `--wiki-type` when not explicitly set:
  `--strip-dots`, `--spaces-to-hyphens`, `--subpages-as-dirs`,
  `--attachment-dir`
- feat: add `--log-file` option to control log file path (default:
  `moin2gitwiki.log` in current directory, also via `MOIN2GIT_LOG_FILE`)
- feat: add `--category-folders` option — uses MoinMoin category tags to
  organize converted pages into subfolders using an incremental category
  tree that tracks hierarchy changes across the full revision history.
  Category pages, category subpages, and regular tagged pages are each
  classified and placed correctly. Cascade renames are emitted when a
  category hierarchy changes. Off by default for backward compatibility.
- feat: strip `Category` prefix from known category names in converted
  content when `--category-folders` is enabled
- feat: add `CategoryTree` with `CategoryNode` and `PageNode` — incremental
  category-to-path resolution updated per revision as wiki history is
  replayed, replacing the former static two-pass `build_category_map()`
  approach
- feat: add `CategoryPlacement` and `category_placement()` / `prev_category_placement()`
  to `MoinEditEntry` — classifies each page as `'category'`, `'subpage'`,
  or `'page'` for routing into the category tree
- fix: RENAME now handled as delete-old + add-new, correctly covering all
  combinations: page↔page, page↔category, category↔category,
  page↔subpage
- fix: category page renames clean up the old category node from the tree
  before creating the new one, cascading child pages to bare-name paths
- fix: falsy empty-string resolved paths no longer silently skip `D`
  commands — all `old_resolved` checks use `is not None`
- fix: bare `"Category"` page name (empty stripped name) treated as a
  regular page, preventing a spurious `.md` file in the output
- fix: leading/trailing spaces stripped from all path components in
  `sanitize_for_path()`, and from category names and suffixes parsed
  from page content
- fix: revision handling unified — CategoryTree always initialized
  regardless of `--category-folders`; plain and category-folders modes
  share a single `add_wiki_revision()` code path
- fix: `markdown_page_name()` and `markdown_page_path()` now return
  category-resolved paths when `--category-folders` is enabled, fixing
  `Home.md` links and attachment paths
- fix: `translate_page` command exits with error and message on stderr
  when the requested page/revision is not found
- feat: replace `--home-page/--no-home-page` with `--home-page
  [none|end|incremental]` — `end` generates Home.md once at the end
  (default), `incremental` updates it as part of every commit that
  changes page paths, `none` skips it entirely
- feat: warn when synthetic Home.md overwrites an existing wiki Home page
- fix: detect primary category from rendered HTML using lxml — switch
  BeautifulSoup parser from `html.parser` to `lxml` (correctly isolates
  unclosed `<p>` tags), replace multiple `find_all` passes with a single
  depth-first traversal; category membership is detected from direct
  children of linemark paragraphs only, ignoring categories in tables
  and prose; self-references skipped for category pages
- fix: local MoinMoin markup no longer read — category detection and
  content translation both operate entirely from rendered HTML
- fix: split `category_placement()` into `name_placement()` (pure name
  classification) and `category_placement(np, primary_category)` to
  thread the self-reference skip name through the call chain before
  HTML is fetched
- fix: replace `getattr(ctx, ...)` with direct attribute access — all
  context attributes have declared defaults in `Moin2GitContext`
- docs: add MoinMoin Preparation section documenting surge protection
  requirement (`surge_action_limits = None`) before running conversion
- docs: update Installation section to reference fork instead of PyPI

<!-- insertion marker -->
[0.8.0] - 2023-04-24
--------------------
[0.7.0] - 2022-11-21
--------------------
- fix: remove hardwired proxy settings
- fix: add recursion limit for beautiful soup
- fix: updated pytest
- fix: change startup to not require --moin-data for check

[0.6.0] - 2021-09-25
--------------------
- Improvements to home page generation
- Fix image linking which could presumably have never worked!
- Add proxy support

[0.5.0] - 2021-02-01
--------------------
- Make home page generation optional
- Handle attachments in the wiki

[0.4.0] - 2021-01-11
--------------------
- Some str/bytes fixes

[0.3.0] - 2021-01-11
--------------------
- Swap out `sh` for `subprocess` module for running pandoc
- Strip extra divs that appear in output

[0.2.0] - 2021-01-06
--------------------
- Initial structure
- Initial CLI structure in place
- Added moin wiki user parser
- Added moin revision parser
- Added basic git fast-import data output - outputs moin markup
- Added fetch cache and initial macro handling
- Directly commit into a new git instance
- Split the wiki translator out into a separate module
- Rebuild the translator to use pandoc on a preprocessed html fragment
- Add a synthetic home page to generated wiki
- Add some docs
