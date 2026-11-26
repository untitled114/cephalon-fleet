"""Pure text sanitising for member-facing output. No discord.py imports."""

import re
from html.parser import HTMLParser

ZWSP = "​"
_ELLIPSIS = "…"

_CLAN_IMAGE = re.compile(r"\{STEAM_CLAN_IMAGE\}\S*")
_BBCODE = re.compile(r"\[/?[A-Za-z*][^\[\]]*\]")
_WHITESPACE = re.compile(r"\s+")
_MENTION_WORDS = re.compile(r"@(?=(?:everyone|here))", re.IGNORECASE)
_MENTION_OPENERS = re.compile(r"<(?=@|#)")
_MARKDOWN = re.compile(r"([\\*_~`|>])")
_SKIP_TAGS = {"script", "style"}
_BREAK_TAGS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BREAK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BREAK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def plain_text(raw: str) -> str:
    text = _CLAN_IMAGE.sub(" ", raw)
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    text = "".join(parser.parts)
    text = _BBCODE.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + _ELLIPSIS if limit > 0 else ""


def excerpt(text: str, limit: int = 280) -> str:
    if len(text) <= limit:
        return text
    window = text[: limit + 1]
    cut = max(window.rfind(" "), window.rfind("\n"), window.rfind("\t"))
    head = window[:cut] if cut > 0 else text[:limit]
    return head.rstrip() + _ELLIPSIS


def neutralise_mentions(text: str) -> str:
    text = _MENTION_WORDS.sub("@" + ZWSP, text)
    return _MENTION_OPENERS.sub("<" + ZWSP, text)


def escape_markdown(text: str) -> str:
    return _MARKDOWN.sub(r"\\\1", text)


FORUM_EXCERPT_CHARS = 900
_VOID_TAGS = {"br", "img", "hr", "meta", "link", "input", "wbr"}
_LIST_TAGS = {"ul", "ol"}
_BLOCK_TAGS = _LIST_TAGS | {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
_STRONG_TAGS = {"strong", "b"}
_ITALIC_TAGS = {"em", "i"}
_NESTED_BULLET = " " * 4 + "◦ "


class _Node:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.children: list[_Node | str] = []


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag)
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(_Node(tag))

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self._stack) - 1, 0, -1):
            if self._stack[depth].tag == tag:
                del self._stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


# One run of text with its emphasis, or None for a <br>.
_Token = tuple[str, bool, bool] | None


def _tokens(nodes: list[_Node | str], strong: bool, italic: bool) -> list[_Token]:
    out: list[_Token] = []
    for node in nodes:
        if isinstance(node, str):
            out.append((node, strong, italic))
        elif node.tag == "br":
            out.append(None)
        elif node.tag in _SKIP_TAGS or node.tag in _VOID_TAGS or node.tag in _LIST_TAGS:
            continue
        else:
            out.extend(
                _tokens(
                    node.children,
                    strong or node.tag in _STRONG_TAGS,
                    italic or node.tag in _ITALIC_TAGS,
                )
            )
    return out


def _flat(tokens: list[_Token]) -> str:
    return _WHITESPACE.sub(" ", "".join(t[0] if t else " " for t in tokens)).strip()


def _all(tokens: list[_Token], index: int) -> bool:
    spoken = [t for t in tokens if t and t[0].strip()]
    return bool(spoken) and all(t[index] for t in spoken)


def _heading(text: str) -> str:
    # The leading newline is the blank line before a heading.
    return f"\n**{text.removesuffix(':').rstrip()}**"


def _paragraph(node: _Node, out: list[str]) -> None:
    tokens = _tokens(node.children, False, False)
    body = tokens
    if None in tokens:
        split = tokens.index(None)
        head = tokens[:split]
        if _all(head, 1):
            out.append(_heading(_flat(head)))
            body = tokens[split + 1 :]
    elif _all(tokens, 1):
        out.append(_heading(_flat(tokens)))
        return
    if body and not _all(body, 2):
        text = _flat(body)
        if text:
            out.append(text)


def _list(node: _Node, depth: int, out: list[str]) -> None:
    for item in node.children:
        if not isinstance(item, _Node) or item.tag != "li":
            continue
        text = _flat(_tokens(item.children, False, False))
        if text:
            out.append(("• " if depth == 0 else _NESTED_BULLET) + text)
        for child in item.children:
            if isinstance(child, _Node) and child.tag in _LIST_TAGS:
                _list(child, depth + 1, out)


def _blocks(nodes: list[_Node | str], out: list[str]) -> None:
    for node in nodes:
        if isinstance(node, str):
            text = _WHITESPACE.sub(" ", node).strip()
            if text:
                out.append(text)
        elif node.tag in _LIST_TAGS:
            _list(node, 0, out)
        elif node.tag in _SKIP_TAGS or node.tag in _VOID_TAGS:
            continue
        elif any(isinstance(c, _Node) and c.tag in _BLOCK_TAGS for c in node.children):
            _blocks(node.children, out)
        else:
            _paragraph(node, out)


def _is_heading(entry: str) -> bool:
    return entry.lstrip("\n").startswith("**") and entry.endswith("**")


def structured_excerpt(raw_html: str, limit: int = FORUM_EXCERPT_CHARS) -> str:
    """Forum HTML as headings, bullets and paragraphs, whole lines only, within limit."""
    builder = _TreeBuilder()
    builder.feed(raw_html)
    builder.close()
    lines: list[str] = []
    _blocks(builder.root.children, lines)
    if lines:
        lines[0] = lines[0].removeprefix("\n")
    entries = lines
    full = "\n".join(entries)
    if len(full) <= limit:
        return full
    if not entries or len(entries[0]) > limit:
        return excerpt(full.split("\n", 1)[0], limit)
    kept = [entries[0]]
    size = len(entries[0])
    for entry in entries[1:]:
        if size + 1 + len(entry) + 2 > limit:
            break
        kept.append(entry)
        size += 1 + len(entry)
    # Never end on a heading that has none of its content under it.
    while len(kept) > 1 and _is_heading(kept[-1]):
        kept.pop()
    return "\n".join(kept) + "\n" + _ELLIPSIS
