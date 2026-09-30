import json
from unittest.mock import Mock

import pytest

import run
from core.archive_compare import compare_archives, comparison_markdown, save_archive_comparison


def topic(name, evidence, summary='analysis', status='analyzed'):
    return {'topic': name, 'raw_evidence': evidence, 'summary': summary, 'status': status}


def source(url, body='USD 10 million', channel='brave'):
    return f'检索渠道: {channel}\n时间: 未提供\n标题: Fund funding\n链接: {url}\n摘要: {body}'


def test_comparison_distinguishes_source_changes_from_topic_changes():
    before = {'topics': [topic('Fund', source('https://example.com/a') + '\n---\n' + source('https://example.com/b')),
                         topic('Old', source('https://example.com/old'))]}
    after = {'topics': [topic('Fund', source('https://example.com/a', 'USD 20 million') + '\n---\n' + source('https://example.com/c')),
                        topic('New', '')]}
    result = compare_archives(before, after)
    assert [row['topic'] for row in result] == ['Fund', 'New', 'Old']
    fund, new, old = result
    assert fund['added_sources'][0]['url'].endswith('/c')
    assert fund['removed_sources'][0]['url'].endswith('/b')
    assert fund['changed_sources'][0]['url'].endswith('/a')
    assert fund['topic_state'] == 'retained' and not fund['analysis_changed']
    assert new['topic_state'] == 'added' and old['topic_state'] == 'removed'
    assert 'fingerprints' not in json.dumps(result)


def test_provider_tracking_and_whitespace_do_not_make_a_new_source():
    a = {'topics': [topic('Fund', source('https://example.com/a?b=2&a=1'))]}
    b = {'topics': [topic('Fund', source('https://example.com/a?a=1&b=2&utm_source=x', channel='parallel'))]}
    row = compare_archives(a, b)[0]
    assert not row['added_sources'] and not row['removed_sources']
    # The raw URL field should not itself count as an updated source excerpt.
    assert not row['changed_sources']
    assert row['unchanged_source_count'] == 1


def test_duplicate_source_complementary_fragments_are_order_independent():
    first, second = source('https://example.com/a', 'first'), source('https://example.com/a', 'second')
    a = {'topics': [topic('Fund', first + '\n---\n' + second)]}
    b = {'topics': [topic('Fund', second + '\n---\n' + first)]}
    row = compare_archives(a, b)[0]
    assert not row['changed_sources'] and row['unchanged_source_count'] == 1


def test_comparison_is_offline_and_does_not_modify_sources(tmp_path, monkeypatch):
    def archive(name, url):
        path = tmp_path / name
        path.write_text(json.dumps({'schema_version': 1, 'title': 'Capital', 'topics': [topic('Fund', source(url))]}))
        return path
    before, after = archive('before.json', 'https://example.com/a'), archive('after.json', 'https://example.com/b')
    originals = [p.read_bytes() for p in (before, after)]
    forbidden = Mock(side_effect=AssertionError('offline comparison invoked research'))
    monkeypatch.setattr(run, 'run_tracker', forbidden)
    monkeypatch.setattr(run, 'SearchService', forbidden)
    monkeypatch.setattr('sys.argv', ['run.py', '--compare-archives', str(before), str(after),
                                   '--output-dir', str(tmp_path / 'out')])
    run.main()
    forbidden.assert_not_called()
    assert originals == [p.read_bytes() for p in (before, after)]
    output = next((tmp_path / 'out').glob('*.md')).read_text()
    assert '不等于新发生投资' in output and '未再收录不等于事件消失' in output
    payload = json.loads(next((tmp_path / 'out').glob('*.json')).read_text())
    assert payload['kind'] == 'source_comparison'


def test_invalid_second_archive_produces_no_comparison(tmp_path):
    before = tmp_path / 'before.json'
    after = tmp_path / 'after.json'
    before.write_text(json.dumps({'schema_version': 1, 'title': 'Capital', 'topics': [topic('Fund', '')]}))
    after.write_text('{}')
    with pytest.raises(ValueError):
        save_archive_comparison(before, after, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


def test_comparison_labels_cannot_break_markdown_tables_or_links():
    rows = compare_archives({'topics': [topic('A|B\nC', '')]},
                            {'topics': [topic('A|B\nC', source('https://example.com/a($X)'))]})
    rows[0]['added_sources'][0]['title'] = 'Label ](https://invented.example)'
    markdown = comparison_markdown(rows, 'before', 'after')
    assert 'A\\|B C' in markdown
    assert '](<https://example.com/a($X)>)' in markdown
