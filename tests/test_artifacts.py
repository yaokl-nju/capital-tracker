"""Evidence archives survive render failures without copying credentials."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config.settings import Config
from core.artifacts import atomic_write, save_report_artifacts
from core.orchestrator import Orchestrator
from core.tracker import Tracker
from trackers.investment import InvestmentQueryGenerator, InvestmentSummarizer


def make_tracker():
    cfg = Config()
    cfg.MAX_QUERIES = 1
    llm = Mock()
    llm.complete.side_effect = ['{"queries":["Fund capital"]}', 'Verified analysis']
    aggregator = Mock()
    aggregator.aggregate.return_value = '链接: https://example.com/event\n摘要: USD 10 million'
    return Tracker(llm, aggregator, InvestmentQueryGenerator(), InvestmentSummarizer(), cfg)


def test_topic_archive_has_exact_input_and_is_a_snapshot():
    tracker = make_tracker()
    assert tracker.process_topic('Fund') == ('Fund', 'Verified analysis')
    record = tracker.snapshot_records()['Fund']
    assert record['queries'] == ['Fund capital']
    assert record['raw_evidence'] == tracker.search_aggregator.aggregate.return_value
    assert record['status'] == 'analyzed' and record['duration_seconds'] >= 0
    record['queries'].clear()
    assert tracker.snapshot_records()['Fund']['queries'] == ['Fund capital']


@pytest.mark.parametrize('raw', ['', '链接: https://example.com/event'])
def test_async_archive_empty_and_model_unavailable(raw):
    tracker = make_tracker()
    async def search(*args):
        return raw
    tracker.perform_search_async = search
    tracker.llm.complete.side_effect = ['{"queries":["q"]}', None]
    asyncio.run(tracker.process_topic_async('Fund'))
    assert tracker.snapshot_records()['Fund']['status'] == ('evidence_only' if raw else 'no_evidence')


def test_topic_failure_records_stage_inputs_without_exception_secret():
    tracker = make_tracker()
    tracker.search_aggregator.aggregate.side_effect = RuntimeError('api_key=secret-test-value')
    _, summary = tracker.process_topic('Fund')
    record = tracker.snapshot_records()['Fund']
    assert record['status'] == 'error' and record['error_type'] == 'RuntimeError'
    assert record['queries'] == ['Fund capital']
    assert 'secret-test-value' not in summary + json.dumps(record)


@pytest.mark.parametrize('stage', ['search', 'summary'])
def test_cancellation_propagates_and_does_not_archive_a_success(monkeypatch, stage):
    tracker = make_tracker()
    async def scenario():
        entered = asyncio.Event()
        async def thread_call(function, *args):
            if function == tracker.summarize and stage == 'summary':
                entered.set()
                await asyncio.Event().wait()
            return function(*args)
        async def search(*args):
            if stage == 'search':
                entered.set()
                await asyncio.Event().wait()
            return '链接: https://example.com/event\n摘要: verified evidence'
        monkeypatch.setattr('core.tracker.asyncio.to_thread', thread_call)
        tracker.perform_search_async = search
        task = asyncio.create_task(tracker.process_topic_async('Fund'))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    record = tracker.snapshot_records()['Fund']
    assert record['status'] == 'error' and record['error_type'] == 'CancelledError'
    assert bool(record['raw_evidence']) == (stage == 'summary')


def test_sidecars_filter_topics_and_do_not_serialize_keys(tmp_path):
    cfg = Config()
    cfg.PARALLEL_SEARCH_API_KEY = 'private-test-key'
    path = tmp_path / 'Capital.v2_001.pdf'
    records = {'Fund': {'queries': ['q'], 'raw_evidence': 'evidence'},
               'Previous run': {'raw_evidence': 'stale'}}
    save_report_artifacts(path, 'Capital', {'Fund': 'analysis'}, records, cfg)
    archive = tmp_path / 'Capital.v2_001.json'
    text = archive.read_text()
    payload = json.loads(text)
    assert payload['topics'][0]['raw_evidence'] == 'evidence'
    assert 'private-test-key' not in text and 'stale' not in text
    assert (tmp_path / 'Capital.v2_001.md').read_text().startswith('# Capital\n')


def test_sidecars_preserved_when_pdf_fails(monkeypatch, tmp_path):
    tracker = make_tracker()
    tracker.process_topic('Fund')
    gen = Mock()
    gen.create_pdf.side_effect = RuntimeError('render failed')
    monkeypatch.setattr('core.orchestrator.ReportGenerator', lambda *a, **kw: gen)
    with pytest.raises(RuntimeError, match='render failed'):
        Orchestrator(tracker).generate_report({'Fund': 'analysis'}, str(tmp_path), 'Capital', 'Capital')
    assert len(list(tmp_path.glob('*.json'))) == 1
    assert len(list(tmp_path.glob('*.md'))) == 1
    payload = json.loads(next(tmp_path.glob('*.json')).read_text())
    assert payload['report_status'] == {'pdf': 'failed', 'error_type': 'RuntimeError'}


def test_all_failed_pipeline_keeps_diagnostic_artifacts(tmp_path):
    tracker = SimpleNamespace(config=Config(), process_topic=lambda t: (t, '处理失败: TimeoutError'))
    with pytest.raises(RuntimeError, match='诊断已保存'):
        Orchestrator(tracker).run(['Fund'], str(tmp_path), 'Capital', 'Capital')
    assert not list(tmp_path.glob('*.pdf'))
    payload = json.loads(next(tmp_path.glob('*_failed.json')).read_text())
    assert payload['topics'][0]['summary'] == '处理失败: TimeoutError'


def test_failed_atomic_replace_keeps_original_and_cleans_temp(monkeypatch, tmp_path):
    target = tmp_path / 'Capital.md'
    target.write_text('previous')
    def fail(*args):
        raise OSError('disk error')
    monkeypatch.setattr('core.artifacts.os.replace', fail)
    with pytest.raises(OSError):
        atomic_write(target, 'new')
    assert target.read_text() == 'previous'
    assert list(tmp_path.iterdir()) == [target]


def test_single_topic_snapshot_is_isolated_and_does_not_copy_other_topics():
    tracker = make_tracker()
    tracker.process_topic('Fund')
    tracker.topic_records['Other'] = {'raw_evidence': 'unrelated evidence'}
    record = tracker.snapshot_record('Fund')
    assert record['raw_evidence'] and 'unrelated' not in json.dumps(record)
    record['queries'].clear()
    assert tracker.snapshot_record('Fund')['queries'] == ['Fund capital']
    assert tracker.snapshot_record('Missing') == {}
