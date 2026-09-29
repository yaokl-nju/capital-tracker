"""Real PDF integration: no external service calls or email sending."""
from pathlib import Path
from pypdf import PdfReader

from core.orchestrator import Orchestrator
from core.report_generator import ReportGenerator
from config.settings import Config


def test_real_pdf_chinese_tables_long_links_and_multiple_topics(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'font-cache'))
    target = tmp_path / 'capital.pdf'
    url = 'https://example.com/disclosure/' + 'long-path-' * 20
    table = '| 事件 | 参与机构及资产 | 金额与币种 | 阶段或方向 | 发生日期 | 披露日期 | 来源链接 |\n'
    table += '|---|---|---|---|---|---|---|\n'
    row = f'| 股权融资 | 测试机构及被投资企业 | USD 100 million | Series B | 未披露 | 2026-09-28 | [机构公告]({url}) |\n'
    contents = {
        '国内资本 & 国际资本': '## 投资与融资\n\n' + table + row * 18,
        '资金流动': '## 检索证据\n\n跨境资本净流入信息待核实。\n\n' + url,
    }
    ReportGenerator(str(target), '金融资本回归测试').create_pdf(contents)
    data = target.read_bytes()
    assert data.startswith(b'%PDF-') and len(data) > 5000
    # PDF engine embeds link annotations and Chinese glyph subsets.
    reader = PdfReader(target)
    text = '\n'.join(page.extract_text() for page in reader.pages)
    assert '金融资本' in text
    assert '2026-09-28' in text
    links = [a.get_object().get('/A', {}).get('/URI')
             for page in reader.pages for a in page.get('/Annots', [])]
    assert url in links
    assert len(reader.pages) >= 2
    assert not list(tmp_path.glob('tmp*.pdf'))


def test_report_generation_failure_preserves_previous_file(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'font-cache'))
    from weasyprint import HTML
    target = tmp_path / 'capital.pdf'
    target.write_bytes(b'previous report')
    def fail(*args, **kwargs):
        raise OSError('disk error')
    monkeypatch.setattr(HTML, 'write_pdf', fail)
    import pytest
    with pytest.raises(RuntimeError, match='PDF 生成失败'):
        ReportGenerator(str(target)).create_pdf({'A': 'test'})
    assert target.read_bytes() == b'previous report'
    assert list(tmp_path.glob('*.pdf')) == [target]


def test_complete_offline_financial_pipeline(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'font-cache'))
    from trackers.investment import InvestmentTracker
    from config.settings import LLMConfig
    from unittest.mock import Mock
    config = Config()
    config.DEEPSEEK = LLMConfig('', 'https://example.com', 'test')
    config.SEC_USER_AGENT = ''
    config.MAX_QUERIES = 2
    config.SEARCH_DELAY_RANGE = (0, 0)
    tracker = InvestmentTracker(config, market='china')
    tracker.search_aggregator.search_service.search = Mock(return_value=[{
        'title': '测试融资公告', 'href': 'https://cninfo.com.cn/test',
        'body': '测试企业完成人民币1亿元融资；测试数据不代表真实交易。', 'date': '2026-09-28',
    }])
    path = Orchestrator(tracker).run(['E Fund'], str(tmp_path), 'China', '国内金融资本', max_workers=1)
    assert Path(path).read_bytes().startswith(b'%PDF-')
    assert tracker.search_aggregator.search_service.search.call_count == 2
    assert tracker.config.SEARCH_GL == 'CN'
