from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import run
from config.settings import Config, LLMConfig
from config.fund_mappings import get_search_names
from core.email_service import EmailConfig, EmailService
from core.llm_service import LLMService
from core.orchestrator import Orchestrator
from core.tracker import Tracker
from trackers.investment import InvestmentQueryGenerator, InvestmentSummarizer, InvestmentTracker


@pytest.fixture
def tracker():
    config = Config()
    config.MAX_TRIALS = 1
    config.MAX_QUERIES = 3
    return Tracker(Mock(), Mock(), InvestmentQueryGenerator(), InvestmentSummarizer(), config)


@pytest.mark.parametrize(('response', 'expected'), [
    ('{"queries":[" first ","second","first",null,42,""]}', ['first', 'second']),
    ('["b","a","b"]', ['b', 'a']),
    ('```json\n{"queries":["valid"]}\n```', ['valid']),
    ('{"holdings":["q1"],"flows":["q2"]}', ['q1', 'q2']),
])
def test_query_parsing_stable_strings(tracker, response, expected):
    tracker.llm.complete.return_value = response
    assert tracker.generate_queries('Fund') == expected


@pytest.mark.parametrize('response', ['{"queries":"not a list"}', '[]', '{}', 'null', 'invalid', None])
def test_invalid_queries_use_financial_fallback(tracker, response):
    tracker.llm.complete.return_value = response
    queries = tracker.generate_queries('Fund')
    assert len(queries) == 3
    assert all(isinstance(q, str) for q in queries)
    assert any('financing' in q for q in queries)


def test_domestic_queries_cover_chinese_aliases_and_flows():
    assert '高毅资产' in get_search_names('Perseverance Asset Management')
    queries = InvestmentQueryGenerator('china').get_fallback_queries('E Fund')
    assert any('易方达' in q and '融资' in q for q in queries)
    assert any('cninfo.com.cn' in q for q in queries)
    assert any('fundraising' in q for q in queries)
    prompt = InvestmentQueryGenerator('china').get_prompt('Springs Capital')
    assert '淡水泉' in prompt and '每个维度的第一条必须使用中文机构名称' in prompt
    assert '基金官网、交易所公告及定期报告' in prompt
    assert '每个维度的第一条必须使用中文机构名称' not in InvestmentQueryGenerator().get_prompt('Springs Capital')


def test_no_summary_is_not_claimed_as_no_events(tracker):
    assert '今日无重大' not in tracker.summarize('Fund', '')
    tracker.search_aggregator.aggregate.side_effect = RuntimeError('backend failure')
    topic, summary = tracker.process_topic('Fund')
    assert topic == 'Fund' and '失败' in summary


def test_llm_empty_key_does_not_attempt_network():
    config = LLMConfig('', 'https://example.com', 'test')
    assert LLMService(config).complete('test') is None


def test_llm_deterministic_auth_failure_not_retried(monkeypatch):
    client = Mock()
    error = RuntimeError('auth')
    error.status_code = 401
    client.chat.completions.create.side_effect = error
    config = LLMConfig('fake', 'https://example.com', 'test')
    monkeypatch.setattr(config, 'create_client', lambda: client)
    service = LLMService(config)
    assert service.complete('test') is None
    assert client.chat.completions.create.call_count == 1


def test_scope_mapping_and_china_run(monkeypatch, tmp_path):
    assert set(run.TRACKER_CLASSES) == {'investment', 'investment_china'}
    for mapping in [run.OUTPUT_DIRS, run.REPORT_TITLES, run.FILENAME_PREFIXES, run.DEFAULT_TOPICS]:
        assert set(mapping) == set(run.TRACKER_CLASSES)
    factory = Mock()
    orchestrator = Mock()
    monkeypatch.setitem(run.TRACKER_CLASSES, 'investment_china', factory)
    monkeypatch.setattr(run, 'Orchestrator', lambda _: orchestrator)
    run.run_tracker('investment_china', ['高毅资产'], str(tmp_path))
    factory.assert_called_once_with(market='china')
    assert orchestrator.run.call_args.kwargs['filename_prefix'] == 'Capital_China'


def test_market_config_isolation():
    config = Config()
    config.DEEPSEEK = LLMConfig('', 'https://example.com', 'test')
    global_tracker = InvestmentTracker(config=config)
    domestic = InvestmentTracker(config=config, market='china')
    assert global_tracker.config.SEARCH_GL == 'US'
    assert domestic.config.SEARCH_GL == 'CN'


def test_orchestrator_order_and_duplicate_topics():
    tracker = SimpleNamespace(config=Config(), process_topic=lambda t: (t, 'summary ' + t))
    orchestrator = Orchestrator(tracker)
    result = orchestrator.run_topics(['B', 'A', 'B'], max_workers=2)
    assert list(result) == ['B', 'A']
    with pytest.raises(ValueError):
        orchestrator.run_topics([], max_workers=1)
    with pytest.raises(ValueError):
        orchestrator.run_topics(['A'], max_workers=0)


def test_pdf_failure_and_email_failure_propagate(monkeypatch, tmp_path):
    tracker = SimpleNamespace(config=Config())
    gen = Mock()
    monkeypatch.setattr('core.orchestrator.ReportGenerator', lambda *a, **kw: gen)
    gen.create_pdf.side_effect = RuntimeError('render failed')
    with pytest.raises(RuntimeError, match='render failed'):
        Orchestrator(tracker).generate_report({'A': 'a'}, str(tmp_path), 'test', 'Test')
    gen.create_pdf.side_effect = None
    sender = Mock()
    sender.send_with_pdf.return_value = False
    monkeypatch.setattr('core.orchestrator.EmailService', lambda _: sender)
    with pytest.raises(RuntimeError, match='邮件发送失败'):
        Orchestrator(tracker).generate_report({'A': 'a'}, str(tmp_path), 'test', 'Test',
                                             True, EmailConfig('sender', 'password'), 'recipient')


def test_email_context_cleanup_and_attachment(monkeypatch, tmp_path):
    config = EmailConfig('sender@example.com', 'fake')
    pdf = tmp_path / '资本.pdf'
    pdf.write_bytes(b'%PDF-test')
    smtp = Mock()
    smtp.__enter__ = Mock(return_value=smtp)
    smtp.__exit__ = Mock(return_value=False)
    monkeypatch.setattr('core.email_service.smtplib.SMTP_SSL', Mock(return_value=smtp))
    assert EmailService(config).send_with_pdf(str(pdf), 'reader@example.com')
    smtp.login.assert_called_once_with('sender@example.com', 'fake')
    assert 'application/pdf' in smtp.sendmail.call_args.args[2]
    assert smtp.__exit__.call_count == 1


def test_report_titles_are_html_escaped(monkeypatch, tmp_path):
    # Render interception avoids requiring native PDF libraries for this unit test.
    from core.report_generator import ReportGenerator
    captured = {}
    def render(self, html, target):
        captured['html'] = html
        Path(target).write_bytes(b'%PDF-mock')
    monkeypatch.setattr(ReportGenerator, '_render_html', render)
    ReportGenerator(str(tmp_path / 'out.pdf'), '<script>title</script>').create_pdf({'A & B': 'Report'})
    assert '&lt;script&gt;title' in captured['html']
    assert 'A &amp; B' in captured['html']
    assert (tmp_path / 'out.pdf').read_bytes() == b'%PDF-mock'


def test_summary_failure_keeps_clickable_evidence(tracker):
    tracker.llm.complete.return_value = None
    data = '标题: Capital event\n链接: https://example.com/filing\n摘要: USD 10 million'
    summary = tracker.summarize('Fund', data)
    assert '原始检索证据' in summary and '[原始来源](https://example.com/filing)' in summary


def test_pdf_blocks_external_resources_and_unsafe_links():
    from core.report_generator import ReportGenerator
    import re
    with pytest.raises(ValueError):
        ReportGenerator._reject_resource('file:///etc/passwd')
    assert re.sub(r'href="([^"]*)"', ReportGenerator._safe_href,
                  'href="file:///private/data"') == ''
    assert re.sub(r'href="([^"]*)"', ReportGenerator._safe_href,
                  'href="https://example.com/a"') == 'href="https://example.com/a"'


def test_unsearched_citation_is_marked_without_discarding_analysis(tracker):
    tracker.llm.complete.return_value = 'Fund invests [source](https://invented.example/new)'
    raw = '链接: https://reuters.com/verified\n摘要: test event'
    summary = tracker.summarize('Fund', raw)
    assert 'Fund invests' in summary and '来源链接未核实' in summary
    assert 'https://invented.example' not in summary
    tracker.llm.complete.return_value = 'Capital event [source](https://reuters.com/verified)'
    assert tracker.summarize('Fund', raw) == tracker.llm.complete.return_value


def test_inline_code_source_citation_becomes_a_clickable_link(tracker):
    tracker.llm.complete.return_value = 'Capital event `[来源](https://reuters.com/verified)`'
    raw = '链接: https://reuters.com/verified\n摘要: test event'
    assert tracker.summarize('Fund', raw) == 'Capital event [来源](https://reuters.com/verified)'


def test_all_failed_topics_are_not_reported_as_success(tmp_path):
    tracker = SimpleNamespace(config=Config(), process_topic=lambda t: (t, '数据处理失败: offline'))
    with pytest.raises(RuntimeError, match='全部主题'):
        Orchestrator(tracker).run(['Fund'], str(tmp_path), 'test', 'Test')
    assert not list(tmp_path.glob('*.pdf'))


def test_email_sender_env_is_separate_from_recipient(monkeypatch, tmp_path):
    factory = Mock()
    orchestrator = Mock()
    monkeypatch.setitem(run.TRACKER_CLASSES, 'investment', factory)
    monkeypatch.setattr(run, 'Orchestrator', lambda _: orchestrator)
    monkeypatch.setattr(run.Config, 'DEFAULT_EMAIL', 'sender@example.com')
    monkeypatch.setattr(run.Config, 'DEFAULT_EMAIL_PASSWORD', 'fake')
    run.run_tracker('investment', ['Fund'], str(tmp_path), send_email=True, email_addr='reader@example.com')
    options = orchestrator.run.call_args.kwargs
    assert options['email_config'].sender_email == 'sender@example.com'
    assert options['recipient_email'] == 'reader@example.com'


def test_alias_lookup_case_and_chinese_reverse():
    from config.fund_mappings import get_sec_edgar_name
    assert get_sec_edgar_name(' himalaya capital（注释） ') == 'Himalaya Capital Management LLC'
    assert get_search_names('易方达基金') == ['E Fund', '易方达基金']
    assert get_search_names('e fund') == ['E Fund', '易方达基金']


@pytest.mark.parametrize('timelimit,days', [('d', 1), ('w', 7), ('m', 31), ('y', 366)])
def test_query_and_summary_windows_follow_config(timelimit, days):
    import datetime
    config = Config()
    config.NEWS_TIMELIMIT = timelimit
    config.MAX_QUERIES = 2
    start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    generator_prompt = InvestmentQueryGenerator(config=config).get_prompt('Fund')
    summary_prompt = InvestmentSummarizer(config=config).get_prompt('Fund', 'data')
    assert start in generator_prompt and start in summary_prompt
    assert '最多采用2条' in generator_prompt
    assert '两端日期包含' in summary_prompt


def test_daily_brief_preserves_evidence_rules_and_does_not_change_full_mode():
    full_config = Config()
    full = InvestmentSummarizer(full_config).get_prompt('Himalaya Capital', 'raw evidence')
    assert '本次输出为每日速览' not in full
    brief_config = Config()
    brief_config.REPORT_STYLE = 'brief'
    brief = InvestmentSummarizer(brief_config).get_prompt('Himalaya Capital', 'raw evidence')
    assert '本次输出为每日速览' in brief and '最多 5 行、4 列' in brief
    for rule in ('第三方来源转述', '时效未核实', '历史快照', '相邻季度基线', '不填充虚构事实'):
        assert rule in brief
    assert '13F 的“持仓比例”仅为本申报表市值占比' in brief
    assert 'raw evidence' in brief and full_config.REPORT_STYLE == 'full'


def test_fallback_chinese_identity_hints_do_not_become_one_exact_phrase():
    queries = InvestmentQueryGenerator().get_fallback_queries('Himalaya Capital')
    assert queries[0].startswith('"喜马拉雅资本" 李录 ')
    assert '"喜马拉雅资本 李录"' not in ' '.join(queries)
    cpe = InvestmentQueryGenerator('china').get_fallback_queries('CPE')
    assert cpe[0].startswith('"中信产业基金" CPE源峰 ')
    unknown = InvestmentQueryGenerator().get_fallback_queries('Unknown Capital')
    assert unknown[0].startswith('"Unknown Capital" ')


@pytest.mark.parametrize('limit,per_dimension', [(1, 1), (5, 1), (12, 3), (50, 8)])
def test_query_generation_prompt_scales_to_query_budget(limit, per_dimension):
    config = Config()
    config.MAX_QUERIES = limit
    prompt = InvestmentQueryGenerator(config=config).get_prompt('Fund')
    assert f'每个维度最多 {per_dimension} 个' in prompt
    assert f'实际搜索最多采用{limit}条' in prompt
