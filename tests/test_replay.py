import json
from unittest.mock import Mock

import pytest
from pypdf import PdfReader

import run
from core.artifacts import load_report_archive, render_report_archive


def archive(tmp_path, **changes):
    payload = {'schema_version': 1, 'generated_at': '2026-09-01T08:00:00+00:00',
               'title': '资本观察', 'settings': {'model': 'old-model'},
               'topics': [{'topic': '高毅资产', 'summary': '### 核心持仓\n\n[来源](https://example.com/event)',
                           'status': 'analyzed', 'raw_evidence': 'original evidence'}]}
    payload.update(changes)
    path = tmp_path / 'Capital.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return path


def test_replay_actual_pdf_preserves_original_and_provenance(tmp_path, monkeypatch):
    source = archive(tmp_path)
    original = source.read_bytes()
    forbidden = Mock(side_effect=AssertionError('offline replay invoked research'))
    monkeypatch.setattr(run, 'run_tracker', forbidden)
    monkeypatch.setattr(run, 'SearchService', forbidden)
    monkeypatch.setattr('sys.argv', ['run.py', '--render-archive', str(source),
                                   '--output-dir', str(tmp_path / 'replayed')])
    run.main()
    forbidden.assert_not_called()
    assert source.read_bytes() == original
    pdf = next((tmp_path / 'replayed').glob('*.pdf'))
    text = ''.join(p.extract_text() for p in PdfReader(pdf).pages)
    assert '离线重生成' in text and '2026-09-01' in text
    assert '高毅资产' in text and '核心持仓' in text
    payload = json.loads(pdf.with_suffix('.json').read_text())
    assert payload['generated_at'] == '2026-09-01T08:00:00+00:00'
    assert payload['settings']['model'] == 'old-model'
    assert payload['topics'][0]['raw_evidence'] == 'original evidence'
    assert payload['replay']['network_used'] is False
    assert pdf.with_suffix('.md').exists()


@pytest.mark.parametrize('changes', [
    {'schema_version': True}, {'schema_version': 99}, {'title': ''}, {'title': []},
    {'topics': []}, {'topics': {}}, {'topics': [{'topic': 'Fund', 'summary': 42}]},
    {'topics': [{'topic': 'Fund', 'summary': ''}, {'topic': 'Fund', 'summary': 'duplicate'}]},
    {'topics': [{'topic': 'Fund', 'summary': 'analysis', 'status': []}]},
    {'topics': [{'topic': 'Fund', 'summary': 'analysis', 'raw_evidence': {}}]},
])
def test_invalid_archive_does_not_write_outputs(tmp_path, changes):
    source = archive(tmp_path, **changes)
    target = tmp_path / 'out'
    with pytest.raises(ValueError):
        render_report_archive(source, target)
    assert not target.exists()


def test_replay_failure_keeps_sidecars(tmp_path, monkeypatch):
    source = archive(tmp_path)
    generator = Mock()
    generator.create_pdf.side_effect = RuntimeError('render failed')
    monkeypatch.setattr('core.report_generator.ReportGenerator', lambda *a, **kw: generator)
    with pytest.raises(RuntimeError, match='render failed'):
        render_report_archive(source)
    assert len(list(tmp_path.glob('*_replay_*.json'))) == 1
    assert len(list(tmp_path.glob('*_replay_*.md'))) == 1
    assert not list(tmp_path.glob('*.pdf'))
    payload = json.loads(next(tmp_path.glob('*_replay_*.json')).read_text())
    assert payload['report_status']['pdf'] == 'failed'


def test_replay_cli_rejects_email_and_research_options(tmp_path, monkeypatch):
    source = archive(tmp_path)
    forbidden = Mock()
    monkeypatch.setattr(run, 'render_report_archive', forbidden)
    for option in [['--email', 'someone@example.com'], ['--topics', 'Fund'], ['--doctor'], ['--dry-run'],
                   ['--no-sec-holdings'], ['--no-evidence-only'], ['--cache'], ['--checkpoints']]:
        monkeypatch.setattr('sys.argv', ['run.py', '--render-archive', str(source), *option])
        with pytest.raises(SystemExit) as exc:
            run.main()
        assert exc.value.code == 2
    forbidden.assert_not_called()


def test_large_archive_is_rejected(tmp_path):
    source = tmp_path / 'large.json'
    with source.open('wb') as handle:
        handle.truncate(32 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match='32 MiB'):
        load_report_archive(source)


def test_replay_markdown_rechecks_links_without_changing_archived_analysis(tmp_path, monkeypatch):
    topic = {'topic': 'Fund', 'summary': '[valid](https://example.com/a) [unknown](https://invented.example/a)',
             'raw_evidence': '链接: https://example.com/a\n摘要: evidence'}
    source = archive(tmp_path, topics=[topic])
    original = source.read_bytes()
    monkeypatch.setattr('core.report_generator.ReportGenerator.create_pdf', lambda *a, **kw: None)
    target = render_report_archive(source)
    from pathlib import Path
    markdown = Path(target).with_suffix('.md').read_text()
    assert '[valid](https://example.com/a)' in markdown
    assert 'invented.example' not in markdown and '来源链接未核实' in markdown
    replay = json.loads(Path(target).with_suffix('.json').read_text())
    assert replay['topics'][0]['summary'] == topic['summary']
    assert source.read_bytes() == original
