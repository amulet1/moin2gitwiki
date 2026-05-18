import re
import subprocess
from pathlib import Path
from typing import Optional

import attr
from bs4 import BeautifulSoup
from furl import furl

from .fetch_cache import FetchCache
from .wikiindex import MoinEditEntries
from .wikiindex import MoinEditEntry


@attr.s(kw_only=True, frozen=True, slots=True)
class Moin2Markdown:
    """
    Conversion object to convert MoinMoin wiki markup to Markdown

    Attributes:
        fetch_cache:    A FetchCache object used to retrieve URLs
        url_prefix:     The URL prefix of the Moin wiki web presence
        revisions:      MoinEditEntries object for link resolution
        ctx:            Context object - logger and user mapping etc
    """

    #
    # -- attributes
    fetch_cache: FetchCache = attr.ib()
    url_prefix: furl = attr.ib()
    revisions: MoinEditEntries = attr.ib()
    ctx = attr.ib(repr=False)
    #
    # smiley mapping
    smiley_map = {
        "X-(": ":rage:",
        ":(": ":confused:",
        ";)": ":wink:",
        ":-?": ":stuck_out_tongue:",
        ":-(": ":frowning_face:",
        ";-)": ":wink:",
        "{X}": ":x:",
        "{3}": ":three:",
        ":D": ":grin:",
        ":)": ":slightly_smiling_face:",
        "/!\\": ":warning:",
        ":\\": ":confounded:",
        ":-)": ":smiley:",
        "|-)": ":pensive:",
        "{i}": ":information_source:",
        "{*}": ":star:",
        "<:(": ":mask:",
        "B)": ":sunglasses:",
        "<!>": ":warning:",
        ">:>": ":imp:",
        "B-)": ":nerd_face:",
        "(./)": ":white_check_mark:",
        "{1}": ":one:",
        "{o}": ":star:",
        ":o": ":anguished:",
        ":))": ":rofl:",
        "(!)": ":bulb:",
        "|)": ":monocle_face:",
        ":-))": ":rofl:",
        "{OK}": ":ok:",
        "{2}": ":two:",
    }

    @classmethod
    def create_translator(
        cls,
        ctx,
        cache_directory: Path,
        url_prefix: str,
        revisions: MoinEditEntries,
    ):
        """
        Build a translator object

        Parameters:
            ctx:              Context object (logger etc)
            cache_directory:  Path object for the cache directory
            url_prefix:       The base URL for the MoinMoin wiki
            revisions:        MoinEditEntries object for link resolution

        """
        #
        # Build a fetch cache
        fetch_cache = FetchCache.initialise_cache(
            cache_directory=cache_directory,
            ctx=ctx,
        )
        return cls(
            fetch_cache=fetch_cache,
            revisions=revisions,
            url_prefix=furl(url_prefix),
            ctx=ctx,
        )

    def retrieve_and_translate(self, revision: MoinEditEntry, skip=None):
        """
        Retrieve a wiki revision, and translate it to markdown

        Parameters:
            revision:    The wiki revision object for the revision we want
            skip:        Category name to skip during detection (for self-reference
                         avoidance on category pages), or None

        Returns a tuple (content, primary_category) where content is the
        translated markdown bytes (or None if the revision has no content),
        and primary_category is the detected primary category name (or None).
        """
        if not revision.wiki_content_path().is_file():
            return None, None
        target = self.url_prefix.copy()
        target /= revision.page_path_unescaped()
        target.args["action"] = "recall"
        target.args["rev"] = revision.page_revision
        content = self.fetch_cache.fetch(target.url)
        main_content, primary_category = self.extract_content_section(content, skip=skip)
        translated = self.translate(main_content)
        # when category-folders is enabled, replace CategoryXxx with Xxx
        # for all known categories so converted pages use clean names
        if self.ctx.category_folders:
            tree = self.ctx.category_tree
            if tree is not None:
                for stripped in tree.category_nodes:
                    translated = translated.replace(
                        f"Category{stripped}".encode(),
                        stripped.encode(),
                    )
        return translated, primary_category

    def extract_content_section(self, html: str, skip=None):
        """
        Extract the content part of the HTML, simplify it, and detect
        the primary category membership.

        Returns a tuple (simplified_html, primary_category) where
        primary_category is the last category link found in a linemark
        paragraph, or None if no category link was found.

        Simplification consists of:
        - stripping out redundant anchor spans
        - remove the additional line marking paragraphs
        - rewrite a/hrefs
        - strip internal a/hrefs that have no existing target
        - strip class attributes from links
        - remap any emoji img to the emoji sequence
        """
        soup = BeautifulSoup(html, "lxml")
        # fix: Explorer theme uses id="page_content", Modern theme uses id="content"
        content = soup.find(id="page_content") or soup.find(id="content")
        if content is None:
            return "", None
        #
        # Single pass over all tags — handle each by type.
        # lxml correctly isolates unclosed <p> tags so each paragraph contains
        # only its own children; depth-first order means parent is visited
        # before its children.
        #
        # Category detection: track the current linemark <p> object so that
        # tag.parent is current_linemark_p correctly identifies direct children
        # of a linemark paragraph. Category links must be direct children of a
        # linemark paragraph — nested links (e.g. inside <strong>) are ignored.
        last_category = None
        current_p_category = None
        current_linemark_p = None
        #
        for tag in content.find_all(True):
            if tag.name == "p":
                # commit previous paragraph's category on entering a new <p>
                if current_p_category is not None:
                    last_category = current_p_category
                    current_p_category = None
                if tag.has_attr("class") and re.match(r"line\d+", tag["class"][0]):
                    current_linemark_p = tag
                    del tag["class"]
                else:
                    current_linemark_p = None

            elif tag.name == "span" and "anchor" in tag.get("class", []):
                tag.decompose()

            elif tag.name == "a":
                if not tag.get("href"):
                    continue
                target = tag["href"]
                self.ctx.logger.debug(f"Trying to map link {target}")
                try:
                    url = self.url_prefix.copy().join(target)
                except ValueError:
                    self.ctx.logger.debug(f"Skipping invalid link {target}")
                    continue
                if url.url.startswith(self.url_prefix.url):
                    new_url = url.copy().remove(query=True).url[len(self.url_prefix.url):]
                    if len(str(url.query)) == 0:
                        # detect category membership — only direct children of a
                        # linemark paragraph count as membership declarations
                        if tag.parent is current_linemark_p and new_url.startswith("Category"):
                            cat_name = new_url[len("Category"):]
                            if not (skip and cat_name.split("/", 1)[0] == skip):
                                if current_p_category is None:
                                    current_p_category = cat_name
                        # conventional link — rewrite or strip
                        new_target = self.revisions.get_new_link_target(new_url)
                        if new_target:
                            tag["href"] = new_target
                            self.ctx.logger.debug(f"Normal map -> {new_target}")
                    elif (
                        "action" in url.query.params
                        and "target" in url.query.params
                        and url.query.params["action"] == "AttachFile"
                    ):
                        attach_target = url.query.params["target"]
                        new_target = self.revisions.get_new_attachment_link_target(
                            new_url, attach_target,
                        )
                        if new_target:
                            tag["href"] = new_target
                            self.ctx.logger.debug(f"Attach map -> {new_target}")
                    else:
                        tag.unwrap()
                        continue  # tag detached — skip class strip
                if tag.has_attr("class"):
                    del tag["class"]

            elif tag.name == "img":
                if not tag.get("src"):
                    continue
                target = tag["src"]
                self.ctx.logger.debug(f"Image target {target}")
                if tag.has_attr("title") and tag["title"] in self.smiley_map:
                    tag.replace_with(" " + self.smiley_map[tag["title"]] + " ")
                    continue
                if target:
                    url = self.url_prefix.copy().join(target)
                    if url.url.startswith(self.url_prefix.url):
                        new_url = url.copy().remove(query=True).url[len(self.url_prefix.url):]
                        self.ctx.logger.debug(f"Image params {url.query.params}")
                        if (
                            "action" in url.query.params
                            and "target" in url.query.params
                            and url.query.params["action"] == "AttachFile"
                        ):
                            attach_target = url.query.params["target"]
                            new_target = self.revisions.get_new_attachment_link_target(
                                new_url, attach_target,
                            )
                            if new_target:
                                tag["src"] = new_target
                                self.ctx.logger.debug(f"Image mapped to {new_target}")
                    else:
                        self.ctx.logger.debug(f"Not mapped - {url.query.params}")
                if tag.has_attr("class"):
                    del tag["class"]

            elif tag.name == "form":
                tag.unwrap()

            elif tag.name == "input":
                tag.decompose()

            elif tag.name == "div":
                tag.unwrap()

        # commit last paragraph's category
        if current_p_category is not None:
            last_category = current_p_category

        return "".join([str(x) for x in content.contents]), last_category

    def translate(self, input: str) -> bytes:
        """Translate HTML to Github Flavoured Markdown using pandoc"""
        process = subprocess.Popen(
            ["pandoc", "-f", "html", "-t", "gfm"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        (output, _) = process.communicate(input.encode("utf-8"))
        return output


# end
