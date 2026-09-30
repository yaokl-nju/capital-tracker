import markdown
import pytest

from core.citations import validate_citations
from core.report_generator import ReportGenerator


GOOD = 'https://example.com/event($ABC)?a=1&b=2'
BAD = 'https://invented.example/event'


@pytest.mark.parametrize('syntax', [
    f'[source]({GOOD})', f'[source](<{GOOD}>)', f'[source]({GOOD} "Title (with parentheses)")',
    f'[source](<{GOOD}> \'Title\')', f'[label [nested]]({GOOD})',
    f'[Source][ref]\n\n[ref]: {GOOD}', f'[ref][]\n\n[ref]: {GOOD}',
    f'[ref]\n\n[ref]: {GOOD}', f'[source][Ref]\n\n[ref]:\n  {GOOD} "Title"',
])
def test_matched_markdown_citation_forms_remain_clickable(syntax):
    result, audit = validate_citations(syntax, [GOOD])
    assert result == syntax and audit == {'citation_count': 1, 'matched_count': 1, 'unverified_count': 0}
    assert 'href=' in markdown.markdown(result)


@pytest.mark.parametrize('syntax', [
    f'[source]({BAD})', f'[source](<{BAD}> "Title")', f'[nested [label]]({BAD})',
    f'[Source][ref]\n\n[ref]: {BAD}', f'[ref][]\n\n[ref]: {BAD}',
    f'[ref]\n\n[ref]:\n {BAD}', '[source](javascript:alert(1))',
])
def test_unmatched_forms_are_marked_without_retaining_clickable_urls(syntax):
    result, audit = validate_citations(syntax, [GOOD])
    assert '来源链接未核实' in result and BAD not in result
    assert 'href=' not in markdown.markdown(result)
    assert audit['unverified_count'] == 1


def test_adjacent_links_cannot_be_swallowed_as_one_url():
    text = f'[A]({GOOD})|[B]({BAD})|[C]({GOOD})'
    result, audit = validate_citations(text, [GOOD])
    assert result.count(GOOD) == 2 and BAD not in result
    assert audit == {'citation_count': 3, 'matched_count': 2, 'unverified_count': 1}


def test_literal_code_and_escaped_brackets_are_not_rewritten():
    text = f'`[code]({BAD})`\n\n```markdown\n[example]({BAD})\n[ref]: {BAD}\n```\n\n\\[literal]({BAD})'
    result, audit = validate_citations(text, [GOOD])
    assert result == text and audit['citation_count'] == 0


def test_tracking_parameters_and_html_entities_match_original_source():
    text = '[source](https://example.com/a?a=1&amp;b=2&utm_source=news)'
    result, audit = validate_citations(text, ['https://example.com/a?b=2&a=1'])
    assert result == text and audit['matched_count'] == 1


def test_rendered_citation_check_handles_multiline_and_nested_markdown():
    text = f'[nested [source]]({BAD})\n\n[other][ref]\n\n[ref]:\n {BAD}\n\n[valid]({GOOD})'
    html = ReportGenerator._validate_rendered_links(markdown.markdown(text), [GOOD])
    assert BAD not in html and 'example.com/event' in html
    assert html.count('来源链接未核实') == 2


def test_malformed_destination_cannot_match_only_a_valid_prefix():
    text = '[source](https://example.com/a untrusted-suffix)'
    result, audit = validate_citations(text, ['https://example.com/a'])
    assert audit['unverified_count'] == 1 and 'href=' not in markdown.markdown(result)


def test_domain_filter_unicode_and_terminal_dot():
    from config.research_profiles import normalize_domains
    from core.search_service import domain_matches
    rule = normalize_domains(['例子.测试'])[0]
    assert domain_matches('news.例子.测试.', rule)
    assert not domain_matches('例子.测试.evil.example', rule)


@pytest.mark.parametrize('parameter', ['regId', 'copyId', 'century', 'notable'])
def test_url_query_names_that_start_like_html_entities_are_not_decoded(parameter):
    url = f'https://example.com/event?cno=6088&{parameter}=499536'
    syntax = f'[source]({url})'
    result, audit = validate_citations(syntax, [url])
    assert result == syntax and audit['matched_count'] == 1


def test_explicit_html_entities_still_match_plain_ampersand_query_names():
    syntax = '[source](https://example.com/event?cno=6088&amp;regId=499536)'
    result, audit = validate_citations(syntax, ['https://example.com/event?cno=6088&regId=499536'])
    assert result == syntax and audit['matched_count'] == 1
