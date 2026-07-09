#!/usr/bin/env python3
"""
add_writing_entry.py — drop a new note/entry into writing.html

No dependencies beyond the Python standard library. Works fully offline,
which matters since you open the site as local files (file://) rather
than through a server.

USAGE
    python3 add_writing_entry.py my_entry.md
    python3 add_writing_entry.py my_entry.md --file /path/to/writing.html
    python3 add_writing_entry.py my_entry.md --preserved
    python3 add_writing_entry.py my_entry.md --toc-parent notes-drafts --toc-label "3.3 Some New Note"

WHAT IT DOES
    1. Reads a markdown file with a tiny front-matter header.
    2. Converts the body (paragraphs, **bold**, *italic*, `code`,
       [links](url), - bullet lists (2-space nesting), > blockquotes,
       and --- rules) into HTML matching the site's existing markup.
    3. Inserts the result as a new <article> at the END of whichever
       section you point it at (e.g. id="notes-drafts", id="commentaries").
    4. Optionally adds a matching link to the "Jump to Section" list.

INPUT FILE FORMAT (my_entry.md)
    title: Some Thoughts on Something
    id: some-thoughts-on-something
    section: notes-drafts
    ---
    Your writing goes here, in markdown-ish plain text.

    - supports
    - simple bullet lists
      - and nested ones

    > blockquotes work too
    > line by line

    Links [like this](https://example.com) and **bold** or *italic* work inline.

RUN IT AGAIN LATER
    Every time you want to add something new, just write a fresh .md file
    in this same front-matter format and run the script again — it always
    appends to the end of the section you name, so ordering is preserved.
"""

import argparse
import html
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


# ----------------------------------------------------------------------
# Locating insertion points in the existing HTML without a full DOM lib
# ----------------------------------------------------------------------

class ElementEndFinder(HTMLParser):
    """Finds the character offsets of the first element whose `id`
    attribute matches `target_id`: start_offset (right after the opening
    tag's '>') and end_offset (right before the matching closing tag)."""

    def __init__(self, target_id, line_offsets):
        super().__init__(convert_charrefs=False)
        self.target_id = target_id
        self.line_offsets = line_offsets
        self.stack = []            # tag names currently open
        self.target_stack_depth = None
        self.start_offset = None
        self.end_offset = None

    def _offset(self):
        line, col = self.getpos()
        return self.line_offsets[line - 1] + col

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if self.target_stack_depth is None and attrs_dict.get("id") == self.target_id:
            tag_text = self.get_starttag_text() or ""
            self.start_offset = self._offset() + len(tag_text)
            self.stack.append(tag)
            self.target_stack_depth = len(self.stack)
            return
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        # self-closing like <img ... /> — never pushed, nothing to do
        pass

    def handle_endtag(self, tag):
        if self.target_stack_depth is not None and len(self.stack) == self.target_stack_depth:
            if self.end_offset is None:
                self.end_offset = self._offset()
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()


def line_offsets_of(text):
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def find_element_span(html_text, target_id):
    parser = ElementEndFinder(target_id, line_offsets_of(html_text))
    parser.feed(html_text)
    parser.close()
    if parser.end_offset is None:
        raise ValueError(f'Could not find an element with id="{target_id}" in the file.')
    return parser.start_offset, parser.end_offset


def find_element_end(html_text, target_id):
    return find_element_span(html_text, target_id)[1]


def find_matching_ul_end(html_text, search_from):
    """Given an offset right after an opening <ul...>, find the offset of
    its matching closing </ul>, accounting for nested <ul> elements."""
    depth = 1
    pos = search_from
    tag_re = re.compile(r"<(/?)ul\b[^>]*>")
    for m in tag_re.finditer(html_text, search_from):
        if m.group(1):  # closing tag
            depth -= 1
            if depth == 0:
                return m.start()
        else:
            depth += 1
    raise ValueError("Could not find a matching </ul> for the table of contents list.")


# ----------------------------------------------------------------------
# Minimal markdown -> HTML (matches the subset already used on the site)
# ----------------------------------------------------------------------

def inline_md(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', text)
    return text


def _build_list_tree(items):
    """items: list of (indent_level, text) -> nested tree of {text, children}"""
    root = []
    stack = [(-1, root)]
    for level, text in items:
        node = {"text": text, "children": []}
        while stack[-1][0] >= level:
            stack.pop()
        stack[-1][1].append(node)
        stack.append((level, node["children"]))
    return root


def _render_list_tree(nodes):
    out = ["<ul>"]
    for node in nodes:
        if node["children"]:
            out.append(f"<li>{inline_md(node['text'])}")
            out.append(_render_list_tree(node["children"]))
            out.append("</li>")
        else:
            out.append(f"<li>{inline_md(node['text'])}</li>")
    out.append("</ul>")
    return "\n".join(out)


def render_list(lines):
    """lines: list of (indent_level, text) bullet items -> nested <ul>"""
    return _render_list_tree(_build_list_tree(lines))


def markdown_to_html(md_text):
    lines = md_text.strip("\n").split("\n")
    blocks = []
    buf = []
    mode = None  # None | 'p' | 'ul' | 'quote'

    def flush():
        nonlocal buf, mode
        if not buf:
            return
        if mode == "p":
            blocks.append(f"<p>{inline_md(' '.join(buf))}</p>")
        elif mode == "ul":
            items = []
            for raw in buf:
                stripped = raw.lstrip(" ")
                indent = (len(raw) - len(stripped)) // 2
                items.append((indent, stripped[2:]))
            blocks.append(render_list(items))
        elif mode == "quote":
            inner = "\n".join(f"<p>{inline_md(l)}</p>" for l in buf)
            blocks.append(f"<blockquote>\n{inner}\n</blockquote>")
        buf = []
        mode = None

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            flush()
            continue
        if line.strip() == "---":
            flush()
            blocks.append("<hr>")
            continue
        if line.startswith("## "):
            flush()
            blocks.append(f"<h4>{inline_md(line[3:].strip())}</h4>")
            continue
        if re.match(r"^\s*[-*]\s+", line):
            if mode != "ul":
                flush()
                mode = "ul"
            buf.append(line)
            continue
        if line.startswith(">"):
            if mode != "quote":
                flush()
                mode = "quote"
            buf.append(line.lstrip(">").strip())
            continue
        if mode != "p":
            flush()
            mode = "p"
        buf.append(line.strip())

    flush()
    return "\n\n".join(blocks)


# ----------------------------------------------------------------------
# Front matter parsing
# ----------------------------------------------------------------------

def parse_entry_file(path):
    text = path.read_text(encoding="utf-8")
    if "---" not in text:
        raise ValueError("Entry file needs a '---' line separating front matter from the body.")
    head, body = text.split("---", 1)
    meta = {}
    for line in head.strip().splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip().lower()] = val.strip()
    for required in ("title", "id", "section"):
        if required not in meta:
            raise ValueError(f'Front matter is missing "{required}:"')
    return meta, body.strip("\n")


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def build_article_html(meta, body_html, preserved):
    title_html = inline_md(meta["title"])
    heading = f'<h3 id="{html.escape(meta["id"])}">{title_html}</h3>'
    if preserved:
        body = f'<div class="preserved">\n{body_html}\n</div>'
    else:
        body = body_html
    return f"\n<hr>\n\n<article>\n  {heading}\n\n  {body}\n</article>\n"


def insert_before(html_text, offset, new_text):
    return html_text[:offset] + new_text + html_text[offset:]


def update_latest_section(index_path, entry_id, entry_title, writing_filename):
    text = index_path.read_text(encoding="utf-8")
    start, end = find_element_span(text, "latest")
    inner = text[start:end]
    marker = "</h2>"
    h2_end = inner.find(marker)
    if h2_end == -1:
        raise ValueError('Expected the "latest" section to start with an <h2> heading.')
    h2_end += len(marker)
    title_html = inline_md(entry_title)
    new_tail = (
        f'\n      <p><a href="{html.escape(writing_filename)}#{html.escape(entry_id)}">'
        f'{title_html}</a> \u2014 newest addition to '
        f'<a href="{html.escape(writing_filename)}">Writing</a>.</p>\n    '
    )
    new_inner = inner[:h2_end] + new_tail
    new_text = text[:start] + new_inner + text[end:]
    index_path.write_text(new_text, encoding="utf-8")
    print(f"Updated the Latest section in {index_path}.")


def main():
    ap = argparse.ArgumentParser(description="Add a new entry to writing.html")
    ap.add_argument("entry", type=Path, help="markdown file with front matter (see script docstring)")
    ap.add_argument("--file", type=Path, default=Path("writing.html"), help="target HTML file")
    ap.add_argument("--preserved", action="store_true",
                     help="wrap the entry in the muted .preserved box (matches 'Why this' / 'Commentaries' style)")
    ap.add_argument("--toc-parent", default=None,
                     help='id of an existing TOC entry to nest under (e.g. "notes-drafts")')
    ap.add_argument("--toc-label", default=None,
                     help='text for the new "Jump to Section" link (used with --toc-parent)')
    ap.add_argument("--index-file", type=Path, default=None,
                     help="path to index.html to update its Latest section (default: look next to --file)")
    ap.add_argument("--skip-latest", action="store_true",
                     help="don't touch index.html's Latest section")
    args = ap.parse_args()

    if not args.file.exists():
        sys.exit(f"Can't find {args.file}")

    meta, body_md = parse_entry_file(args.entry)
    body_html = markdown_to_html(body_md)
    article_html = build_article_html(meta, body_html, args.preserved)

    site_html = args.file.read_text(encoding="utf-8")

    try:
        section_end = find_element_end(site_html, meta["section"])
    except ValueError as e:
        sys.exit(f"Error: {e}")

    site_html = insert_before(site_html, section_end, article_html)

    if args.toc_parent and args.toc_label:
        anchor_needle = f'href="#{args.toc_parent}"'
        idx = site_html.find(anchor_needle)
        if idx == -1:
            print(f'Note: could not find a TOC link to "#{args.toc_parent}" — skipping TOC update.')
        else:
            a_close = site_html.find("</a>", idx)
            ul_open_match = re.search(r"\s*<ul>", site_html[a_close:a_close + 200])
            new_li = f'\n            <li><a href="#{html.escape(meta["id"])}">{inline_md(args.toc_label)}</a></li>'
            if ul_open_match and ul_open_match.start() < 40:
                ul_start = a_close + ul_open_match.end()
                ul_end = find_matching_ul_end(site_html, ul_start)
                site_html = insert_before(site_html, ul_end, new_li + "\n          ")
            else:
                print(f'Note: "#{args.toc_parent}" has no nested list yet — add the TOC link manually if you want one.')

    args.file.write_text(site_html, encoding="utf-8")
    print(f'Added "{meta["title"]}" to section "{meta["section"]}" in {args.file}.')

    if not args.skip_latest:
        index_path = args.index_file if args.index_file else (args.file.parent / "index.html")
        if index_path.exists():
            try:
                update_latest_section(index_path, meta["id"], meta["title"], args.file.name)
            except ValueError as e:
                print(f"Note: could not update the Latest section — {e}")
        else:
            print(f'Note: no index.html found at "{index_path}" — pass --index-file or --skip-latest.')


if __name__ == "__main__":
    main()
