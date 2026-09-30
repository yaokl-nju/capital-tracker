"""Audit Markdown links against source URLs, including references and parentheses."""
import re
from html import unescape

from .search_service import canonical_url


def _reference_key(label):
    return re.sub(r'\s+', ' ', label.strip()).casefold()


def _destination(text):
    text = text.strip()
    if text.startswith('<'):
        end = text.find('>')
        value = text[1:end] if end >= 0 else ''
        rest = text[end + 1:].strip() if end >= 0 else ''
    else:
        parts = text.split(None, 1)
        value = parts[0] if parts else ''
        rest = parts[1].strip() if len(parts) == 2 else ''
    if rest and not re.fullmatch(r'(?:".*"|\'.*\')', rest, re.S):
        return ''
    value = re.sub(r'\\([!"#$%&\'()*+,\-./:;<=>?@\[\]\\^_`{|}~])', r'\1', value)
    # html.unescape accepts entity prefixes without semicolons (e.g. &regId),
    # corrupting legitimate URL query names. Decode explicit entities only.
    return re.sub(r'&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);',
                  lambda match: unescape(match[0]), value)


def _inline_end(text, opening):
    """Find the closing link delimiter without consuming the next adjacent link."""
    depth, quote, angle, escaped = 1, None, False, False
    for pos in range(opening + 1, len(text)):
        char = text[pos]
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if angle:
            if char == '>':
                angle = False
            continue
        if char == '<':
            angle = True
        elif char in ('"', "'") and pos > opening + 1 and text[pos - 1].isspace():
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return pos
        elif char == '\n' and depth == 1:
            return None
    return None


def _label_end(text, opening):
    depth, escaped = 1, False
    for pos in range(opening + 1, len(text)):
        char = text[pos]
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
        elif char == '[':
            depth += 1
        elif char == ']':
            depth -= 1
            if not depth:
                return pos
        elif char == '\n':
            return None
    return None


def _code_mask(text):
    """Keep offsets but hide fenced and inline code from citation processing."""
    chars = list(text)
    fence, start, offset = None, 0, 0
    ranges = []
    for line in text.splitlines(keepends=True):
        match = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line.rstrip('\n'))
        if match and fence is None:
            fence, start = match[1], offset
        elif match and fence and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
            ranges.append((start, offset + len(line)))
            fence = None
        offset += len(line)
    if fence:
        ranges.append((start, len(text)))
    for start, end in ranges:
        for i in range(start, end):
            if chars[i] != '\n':
                chars[i] = ' '
    masked = ''.join(chars)
    for match in re.finditer(r'(`+)(?!`)(.*?)\1(?!`)', masked, re.S):
        for i in range(match.start(), match.end()):
            if chars[i] != '\n':
                chars[i] = ' '
    return ''.join(chars)


def validate_citations(text, allowed_urls):
    """Keep matched links; replace unmatched inline and reference links with labels."""
    allowed = {url for value in allowed_urls if (url := canonical_url(value))}
    references = {}
    definition = re.compile(r'^ {0,3}\[([^\]\n]+)\]:[ \t]*(?:\n[ \t]*)?(<[^>\n]+>|\S+)(?:[^\n]*)$', re.M)
    edits = []
    for match in definition.finditer(_code_mask(text)):
        key = _reference_key(match[1])
        url = _destination(match[2])
        # Markdown resolves the first reference definition, not the last one.
        if key not in references:
            references[key] = url
        if canonical_url(url) not in allowed:
            edits.append((match.start(), match.end()))

    for start, end in reversed(edits):
        text = text[:start] + text[end:]
    masked = _code_mask(text)
    masked = definition.sub(lambda match: ''.join('\n' if c == '\n' else ' ' for c in match[0]), masked)
    totals = {'citation_count': 0, 'matched_count': 0, 'unverified_count': 0}
    pieces, cursor = [], 0
    label_pattern = re.compile(r'(?<!!)\[')
    while match := label_pattern.search(masked, cursor):
        start = match.start()
        position = start - 1
        while position >= 0 and text[position] == '\\':
            position -= 1
        slashes = start - position - 1
        close_label = _label_end(text, start) if slashes % 2 == 0 else None
        if close_label is None:
            pieces.append(text[cursor:match.end()])
            cursor = match.end()
            continue
        label = text[start + 1:close_label]
        end, url = close_label + 1, None
        if end < len(text) and text[end] == '(':
            close = _inline_end(text, end)
            if close is not None:
                url, end = _destination(text[end + 1:close]), close + 1
        elif end < len(text) and text[end] == '[':
            ref = re.match(r'\[([^\]\n]*)\]', text[end:])
            if ref:
                key = _reference_key(ref[1] or label)
                if key in references:
                    url, end = references[key], end + ref.end()
        elif _reference_key(label) in references:
            url = references[_reference_key(label)]
        if url is None:
            pieces.append(text[cursor:end])
            cursor = end
            continue
        totals['citation_count'] += 1
        valid = canonical_url(url) in allowed
        totals['matched_count' if valid else 'unverified_count'] += 1
        pieces.append(text[cursor:match.start()])
        pieces.append(text[start:end] if valid else label.replace('[', r'\[').replace(']', r'\]') + '（来源链接未核实）')
        cursor = end
    pieces.append(text[cursor:])
    return ''.join(pieces), totals
