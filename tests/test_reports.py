"""Real PDF integration: no external service calls or email sending."""
from pathlib import Path
from pypdf import PdfReader
import pytest

from core.orchestrator import Orchestrator
from core.report_generator import ReportGenerator
from config.settings import Config


@pytest.mark.parametrize('indent', [2, 3, 4])
def test_pdf_stock_list_keeps_child_details_nested_and_fenced_code_literal(monkeypatch, tmp_path, indent):
    import markdown
    from xml.etree import ElementTree
    factory = markdown.Markdown
    converted = []
    def capture(*args, **kwargs):
        parser = factory(*args, **kwargs)
        convert = parser.convert
        def record(text):
            result = convert(text)
            converted.append(result)
            return result
        parser.convert = record
        return parser
    monkeypatch.setattr('core.report_generator.markdown.Markdown', capture)
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'font-cache'))
    child = ' ' * indent
    text = (f'1. **PDD**\n{child}- 季度：2026 Q2\n{child}- 理由：未披露\n\n'
            f'2. **Berkshire**\n{child}- 数量增加\n\n```text\n  - literal code\n```\n')
    target = tmp_path / 'nested.pdf'
    ReportGenerator(str(target), '金融持仓列表').create_pdf({'Fund': text})
    tree = ElementTree.fromstring('<root>' + converted[0] + '</root>')
    assert len(tree.findall('./ol/li')) == 2
    assert len(tree.findall('./ol/li/ul/li')) == 3
    assert tree.find('./pre/code').text == '  - literal code\n'
    assert target.read_bytes().startswith(b'%PDF-')


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
        'body': '易方达参与的测试企业完成人民币1亿元融资；测试数据不代表真实交易。', 'date': '2026-09-28',
    }])
    path = Orchestrator(tracker).run(['E Fund'], str(tmp_path), 'China', '国内金融资本', max_workers=1)
    assert Path(path).read_bytes().startswith(b'%PDF-')
    import json
    archive = json.loads(Path(path).with_suffix('.json').read_text())
    assert archive['topics'][0]['status'] == 'evidence_only'
    assert '人民币1亿元' in archive['topics'][0]['raw_evidence']
    assert Path(path).with_suffix('.md').exists()
    assert tracker.search_aggregator.search_service.search.call_count == 2
    assert tracker.config.SEARCH_GL == 'CN'
