"""One parse per source text per process (ask #2108): a test that parses the daemon entry for every subTest pays for the parse once.

`cached_parse` keeps the last few trees by the hash of the text; `lines_of` keeps the split lines of a text, and `cached_segment` gives a node's source by slicing them
(the whole text is split once, not once per node as `ast.get_source_segment` does). `CachedParse` is a test mixin that makes `ast.parse` and
`ast.get_source_segment` these two for the length of each test, so the product code the test calls (it parses a text it is handed) hits the cache too.
The trees are shared and read only: nothing here edits one."""
import ast
import hashlib
from unittest import mock

_REAL_PARSE = ast.parse
_KEEP = 6
_trees = {}   # key -> tree, oldest first
_lines = {}   # key -> list of lines, oldest first


def _key(source):
    return hashlib.blake2b(source.encode("utf-8", "surrogatepass"), digest_size=16).digest()


def _remember(table, key, value):
    table.pop(key, None)
    table[key] = value
    while len(table) > _KEEP:
        del table[next(iter(table))]
    return value


def cached_parse(source, filename="<unknown>", mode="exec", **kwargs):
    """`ast.parse`, cached for a text parsed with the plain arguments; a SyntaxError is raised again each time, never cached."""
    if not isinstance(source, str) or filename != "<unknown>" or mode != "exec" or kwargs:
        return _REAL_PARSE(source, filename, mode, **kwargs)
    key = _key(source)
    tree = _trees.get(key)
    if tree is None:
        tree = _remember(_trees, key, _REAL_PARSE(source))
    return tree


def lines_of(source):
    """The lines of `source`, split on \\n, \\r\\n and \\r as ast does, each with its ending."""
    key = _key(source)
    got = _lines.get(key)
    if got is None:
        got = []
        pos = 0
        while pos < len(source):
            end = pos
            while end < len(source) and source[end] not in "\r\n":
                end += 1
            if end < len(source):
                end += 2 if source.startswith("\r\n", end) else 1
            got.append(source[pos:end])
            pos = end
        _remember(_lines, key, got)
    return got


def cached_segment(source, node, *, padded=False):
    """`ast.get_source_segment(source, node)`: the node's text by lineno/end_lineno slicing of the cached lines."""
    try:
        if node.end_lineno is None or node.end_col_offset is None:
            return None
        lineno, end_lineno = node.lineno - 1, node.end_lineno - 1
        col, end_col = node.col_offset, node.end_col_offset
    except AttributeError:
        return None
    lines = lines_of(source)
    if lineno == end_lineno:
        return lines[lineno].encode()[col:end_col].decode()
    first = lines[lineno].encode()[col:].decode()
    if padded:
        first = " " * len(lines[lineno].encode()[:col].decode()) + first
    last = lines[end_lineno].encode()[:end_col].decode()
    return "".join([first] + lines[lineno + 1:end_lineno] + [last])


class CachedParse:
    """Mixin, first among the bases: for each test `ast.parse` and `ast.get_source_segment` are the cached ones."""

    def setUp(self):
        for name, fake in (("parse", cached_parse), ("get_source_segment", cached_segment)):
            patcher = mock.patch.object(ast, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()
