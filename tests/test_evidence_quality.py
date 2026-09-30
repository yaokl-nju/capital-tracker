from core.evidence_quality import evidence_index, evidence_quality, coverage_summary


RAW = '''检索渠道: brave
时间: 2026-09-29
信源: Example
标题: Fund investment
链接: https://example.com/a
摘要: USD 10 million

---
检索渠道: gdelt
时间: 未提供（时效未核实）
信源: News
标题: Fund fundraising
链接: https://news.example.com/b
摘要: 仅标题，未读取正文

---
证据用途: 历史比较基线，不计入已核实时效资料条数，不作为近期新闻。
时间: 2026-05-15
信源: SEC
标题: Fund 13F
链接: https://www.sec.gov/Archives/example.xml
摘要: prior quarter
'''


def test_evidence_coverage_does_not_count_baseline_as_recent_or_title_as_body():
    index = evidence_index(RAW)
    assert [i['id'] for i in index] == ['E001', 'E002', 'E003']
    quality = evidence_quality(index)
    assert quality == {'evidence_fragments': 3, 'unique_sources': 3,
                       'source_domains': ['example.com', 'news.example.com', 'www.sec.gov'],
                       'current_fragments': 2, 'dated_fragments': 1, 'undated_fragments': 1,
                       'historical_baselines': 1, 'title_only_fragments': 1}


def test_coverage_uses_exact_evidence_instead_of_trusting_stale_index():
    record = {'status': 'analyzed', 'raw_evidence': RAW, 'evidence_index': [{'bogus': True}]}
    summary = coverage_summary({'Fund | A\nB': 'analysis'}, {'Fund | A\nB': record})
    assert 'Fund \\| A B' in summary
    assert '| 已生成分析 | 3 | 1 | 1 | 1 |' in summary
    assert '不代表逐项事实核验' in summary


def test_invalid_sources_are_excluded_and_duplicate_urls_count_once():
    raw = '链接: file:///private/file\n---\n链接: https://example.com/a\n---\n链接: https://example.com/a'
    quality = evidence_quality(evidence_index(raw))
    assert quality['evidence_fragments'] == 2 and quality['unique_sources'] == 1
    assert evidence_quality([])['undated_fragments'] == 0


def test_unrecorded_tracker_does_not_invent_coverage():
    assert coverage_summary({'Fund': 'analysis'}, {}) == ''


def test_topic_notice_is_computed_from_input_not_model_claims():
    from core.evidence_quality import topic_notice
    notice = topic_notice({'status': 'analyzed', 'raw_evidence': RAW,
                           'evidence_quality': {'unique_sources': 999}})
    assert '输入 3 个来源链接' in notice and '带日期 1 片段' in notice
    assert '日期未知 1 片段' in notice and '历史基线 1 片段' in notice
    assert '999' not in notice and '不表示已经核验原文事实' in notice
