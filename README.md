# MoinMoin To Git (Markdown) Wiki Converter

[![ci](https://img.shields.io/travis/com/amulet1/moin2gitwiki.svg)](https://travis-ci.com/amulet1/moin2gitwiki)
[![documentation](https://img.shields.io/badge/docs-mkdocs%20material-blue.svg?style=flat)](https://amulet1.github.io/moin2gitwiki/)

> **Fork notice:** This is a fork of the original moin2gitwiki by Nigel
> Metheringham, extended with support for additional target wiki platforms,
> category-folder organisation, and various fixes from real-world usage.

App to convert a MoinMoin wiki file tree into a git based wiki as used on
github, gitlab or gitea.

## Current Version

Version: `0.9.0-alpha`

## Status

Actively maintained fork. The original project appears to be abandoned.
Contributions and bug reports are welcome via the GitHub repository.

## Translation Method

Originally the intention was to translate purely by converting the MoinMoin
markup to markdown markup - using the MoinMoin data retrieved from the
filesystem.

However, although it makes determining the overall page list and revision list
much easier, it was found that translating the wiki markup at this level was
too complex and fragile for this to work without a huge amount of special
casing.

So, after the revision structure is derived from the filesystem, each page
revision is retrieved by http requests to the running MoinMoin wiki. This is
then reduced to just the page content (by picking out the content div from the
html), and some light editing applied to simplify the HTML - specifically:-

- Remove the anchor spans that MoinMoin adds - these add no visual or
  readable content, but confuse the translator
- Remove paragraph entries with CSS classes that start `line` - these
  again appear to be for non-required purposes (likely for showing diffs
  between revisions) - and they break the translator
- Fix links that point within the wiki - if the target does not exist
  then the text is left but the link removed.
- Strips CSS classes off links - again these upset the translator
- Translate any images that appear to be MoinMoin emoji characters (which
  are rendered as images) into gollum emoji characters

This simplified HTML is then passed through the pandoc command:-

    pandoc -f html -t gfm

And the resulting Github flavoured Markdown is taken as the new form.

This handles the vast majority of normal markup correctly, including lists and
many types of tables. Some complicated markup or complex tables end up being
passed through as HTML - which displays correctly but is less easy to parse
and edit.

Attachments that are available in the wiki are also handled - they are put
into a directory under a subdirectory named for the original page. The exact
location depends on the `--wiki-type` setting — see Wiki Types below.
Links to attachments should be handled correctly.

## Wiki Types

The `fast-export` command accepts a `--wiki-type` option to configure the
output for different target wiki platforms:

- `gollum` (default) — for GitHub, GitLab, and Gollum-based wikis
- `gitea` — alias for `gollum`, provided for self-documentation
- `otterwiki` — for [Otter Wiki](https://otterwiki.com)

The wiki type sets defaults for the following flags, each of which can be
overridden individually:

| Flag | `gollum` / `gitea` | `otterwiki` | Effect |
|---|---|---|---|
| `--strip-dots` / `--no-strip-dots` | False | **True** | Remove dots from page names |
| `--spaces-to-hyphens` / `--no-spaces-to-hyphens` | **True** | False | Replace spaces with hyphens |
| `--subpages-as-dirs` / `--no-subpages-as-dirs` | False | **True** | MoinMoin `(2f)` subpages as real subdirectories |
| `--attachment-dir` | `_attachments` | `a` | Attachment folder name |

Attachment layout is determined by `--subpages-as-dirs`:

- `True` (otterwiki default): `PageName/<attachment-dir>/file` — alongside the page
- `False` (gollum default): `<attachment-dir>/PageName/file` — central folder

## Category Folders

The `--category-folders` option organises converted pages into subfolders
based on their MoinMoin category membership. Off by default.

When enabled, category pages become folder index pages and tagged pages are
placed under their category's resolved path. Subcategory hierarchies are
derived from the category pages' own content. The category tree is maintained
incrementally across the full revision history, so moves and renames are
reflected correctly in the git history.

For example, a page tagged with `CategoryIT` would be placed at
`<resolved-path-of-IT>/<page-name>.md`.

The `Category` prefix is also stripped from category names in converted page
content when this option is enabled.

## Home Page

The `--home-page` option controls generation of a synthetic `Home.md` page
that lists all wiki pages:

- `end` (default) — generate once at the end, reflecting the final wiki state
- `incremental` — update `Home.md` as part of every commit that changes page
  paths, so each point in history has an accurate home page
- `none` — skip home page generation entirely

A warning is emitted if the synthetic `Home.md` overwrites an existing wiki
`Home` page. Use `--home-page=none` to preserve the original.

## MoinMoin Preparation

Before running the conversion, the MoinMoin instance must be accessible
via HTTP. One temporary change to `wikiconfig.py` is required to disable
surge protection — moin2gitwiki fetches pages rapidly and will otherwise
be blocked, producing empty pages silently:

```python
surge_action_limits = None
```

Restart MoinMoin after making this change. Remember to revert it once
the conversion is complete.

## Issues

The overall process is not particularly fast. But this should be something
you only do once (or a few attempts) so raw speed is not needed.

Attachments are not versioned by MoinMon. This means any attachment that was
deleted from MoinMoin is no longer available to put into the converted wiki.
Any attachment that was updated a few times is only available in the last
version (but will probably be inserted into the history at the point where it
first appeared but with the latest content).

## Installation

This is a fork of the original moin2gitwiki. Install from this repository
rather than from PyPI to get the fixes and features described above.

You will also need `pandoc` and `git` available in your PATH.

    git clone https://github.com/amulet1/moin2gitwiki.git
    cd moin2gitwiki
    python3 -m venv venv
    venv/bin/pip install -e .

To update to the latest version:

    cd moin2gitwiki
    git pull

No reinstall is needed after `git pull` since the package is installed
in editable mode.

## Todo

- Make tests effective

## Changes

See [CHANGELOG.md](CHANGELOG.md) for the full list of changes in this fork.

---
