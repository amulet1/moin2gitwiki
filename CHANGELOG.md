# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/en/1.0.0/)
and this project adheres to [Semantic Versioning](http://semver.org/spec/v2.0.0.html).

Unreleased Changes
------------------

### Fork changes

- fix: regex bug in `is_a_linemark_para()` — `r"line\\d+"` was matching a
  literal backslash followed by `d` rather than a digit, corrected to `r"line\d+"`
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
- feat: add `--wiki-type [gollum|gitea|otterwiki]` option to `fast-export`
  (default: `gollum`) — sets appropriate defaults for page naming, subpage
  structure and attachment layout per target platform
- feat: add fine-grained conversion flags to `fast-export`, all defaulting
  to `None` and derived from `--wiki-type` when not explicitly set:
  `--strip-dots`, `--spaces-to-hyphens`, `--subpages-as-dirs`,
  `--attachment-dir`

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
