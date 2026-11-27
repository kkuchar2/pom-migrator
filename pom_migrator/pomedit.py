"""Formatting-preserving pom.xml editing.

The XML is never re-serialized: elements are located on a copy of the text in which
comments/CDATA/processing instructions are blanked out (same length), and edits are
applied as text splices. Comments, blank lines, indentation and attribute order survive.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

_HIDDEN = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<!DOCTYPE[^>]*>", re.S)
_TAG = re.compile(r"<(/?)([A-Za-z_][\w.\-:]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>")


class PomEditError(Exception):
    pass


def _mask(text: str) -> str:
    return _HIDDEN.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


@dataclass
class Node:
    name: str
    start: int                 # index of '<' of the opening tag
    end: int                   # index after '>' of the closing tag
    inner_start: int
    inner_end: int
    children: list["Node"] = field(default_factory=list)

    def child(self, name: str) -> "Node | None":
        return next((c for c in self.children if c.name == name), None)

    def all(self, name: str) -> list["Node"]:
        return [c for c in self.children if c.name == name]

    def path(self, *names: str) -> "Node | None":
        node: Node | None = self
        for name in names:
            node = node.child(name) if node else None
        return node


def _local(name: str) -> str:
    return name.rsplit(":", 1)[-1]


def parse_tree(text: str) -> Node:
    masked = _mask(text)
    stack: list[Node] = []
    root: Node | None = None
    for m in _TAG.finditer(masked):
        closing, name, _attrs, self_closing = m.group(1), _local(m.group(2)), m.group(3), m.group(4)
        if closing:
            if not stack or stack[-1].name != name:
                raise PomEditError(f"unexpected </{name}> at offset {m.start()}")
            node = stack.pop()
            node.inner_end = m.start()
            node.end = m.end()
            continue
        node = Node(name, m.start(), m.end(), m.end(), m.end())
        if stack:
            stack[-1].children.append(node)
        elif root is None:
            root = node
        if not self_closing:
            stack.append(node)
    if root is None or stack:
        raise PomEditError("unbalanced XML")
    return root


class PomEditor:
    """Collects splices against the original text and applies them at once."""

    def __init__(self, text: str):
        self.text = text
        self._masked = _mask(text)  # comments blanked: a line holding only element + comment counts as "alone"
        self.root = parse_tree(text)
        if self.root.name != "project":
            raise PomEditError("root element is not <project>")
        self._edits: list[tuple[int, int, str]] = []
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.step = self._detect_step()

    # ------------------------------------------------------------------ reading
    def value(self, node: Node | None) -> str | None:
        if node is None:
            return None
        return self.text[node.inner_start:node.inner_end].strip() or None

    def indent_of(self, node: Node) -> str:
        line_start = self.text.rfind("\n", 0, node.start) + 1
        prefix = self.text[line_start:node.start]
        return prefix if prefix.strip() == "" else ""

    def _detect_step(self) -> str:
        for child in self.root.children:
            indent = self.indent_of(child)
            if indent:
                return indent
        return "    "

    # ------------------------------------------------------------------ editing
    def _add(self, start: int, end: int, replacement: str) -> None:
        for s, e, _ in self._edits:
            if start < e and s < end:
                raise PomEditError("overlapping edits")
        self._edits.append((start, end, replacement))

    def set_value(self, node: Node, value: str) -> None:
        self._add(node.inner_start, node.inner_end, value)

    def remove(self, node: Node) -> None:
        """Remove an element; if it is alone on its line(s), remove the whole line."""
        start, end = node.start, node.end
        line_start = self.text.rfind("\n", 0, start) + 1
        line_end = self.text.find("\n", end)
        line_end = len(self.text) if line_end == -1 else line_end + 1
        if self._masked[line_start:start].strip() == "" and self._masked[end:line_end].strip() == "":
            start, end = line_start, line_end
        self._add(start, end, "")

    def _block(self, lines: list[str], indent: str) -> str:
        return "".join(f"{indent}{line}{self.newline}" if line else self.newline for line in lines)

    def insert_child(self, parent: Node, lines: list[str]) -> None:
        """Append lines (already relatively indented with self.step) as last children of parent."""
        child_indent = self.indent_of(parent) + self.step
        close_line_start = self.text.rfind("\n", 0, parent.inner_end) + 1
        if self.text[close_line_start:parent.inner_end].strip() == "":
            self._add(close_line_start, close_line_start, self._block(lines, child_indent))
        else:  # closing tag shares a line with content
            self._add(parent.inner_end, parent.inner_end, self.newline + self._block(lines, child_indent) + self.indent_of(parent))

    def insert_before(self, sibling: Node, lines: list[str], blank_after: bool = True) -> None:
        indent = self.indent_of(sibling)
        line_start = self.text.rfind("\n", 0, sibling.start) + 1
        block = self._block(lines, indent) + (self.newline if blank_after else "")
        self._add(line_start, line_start, block)

    def insert_after(self, sibling: Node, lines: list[str]) -> None:
        indent = self.indent_of(sibling)
        line_end = self.text.find("\n", sibling.end)
        pos = len(self.text) if line_end == -1 else line_end + 1
        self._add(pos, pos, self._block(lines, indent))

    def result(self) -> str:
        out = self.text
        for start, end, replacement in sorted(self._edits, key=lambda e: e[0], reverse=True):
            out = out[:start] + replacement + out[end:]
        try:
            ET.fromstring(out.encode("utf-8"))
        except ET.ParseError as exc:  # never write a broken POM
            raise PomEditError(f"edit produced invalid XML: {exc}") from None
        return out

    @property
    def changed(self) -> bool:
        return bool(self._edits)

    def indent_lines(self, lines: list[str], levels: int) -> list[str]:
        return [(self.step * levels + line) if line else line for line in lines]


def dependency_lines(group_id: str, artifact_id: str, version: str, step: str) -> list[str]:
    return [
        "<dependency>",
        f"{step}<groupId>{group_id}</groupId>",
        f"{step}<artifactId>{artifact_id}</artifactId>",
        f"{step}<version>{version}</version>",
        "</dependency>",
    ]
