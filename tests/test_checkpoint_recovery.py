import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import run
from config.settings import Config
from core.artifacts import load_report_archive, save_report_artifacts
from core.checkpoint_recovery import assemble_checkpoint_archive, save_batch_manifest, topic_filename
from core.orchestrator import Orchestrator


def checkpoint(directory, topic, **changes):
    path = directory / topic_filename(topic)
    save_report_artifacts(path.with_suffix('.pdf'), '研究（单主题检查点）',
                          {topic: '已完成分析'}, {topic: {'status': 'analyzed', 'raw_evidence': ''}},
                          Config(), render_status='not_requested')
    if changes:
        payload = json.loads(path.read_text())
        payload.update(changes)
        path.write_text(json.dumps(payload))
    return path


def test_manifest_recovery_preserves_order_and_distinguishes_missing_topics(tmp_path):
    batch = tmp_path / 'batch'
    batch.mkdir()
    save_batch_manifest(batch, '研究', ['first', 'missing', 'last'])
    first, last = checkpoint(batch, 'first'), checkpoint(batch, 'last')
    originals = {p: p.read_bytes() for p in (first, last)}
    target = assemble_checkpoint_archive(batch, tmp_path / 'recovered')
    payload = load_report_archive(target)
    assert [r['topic'] for r in payload['topics']] == ['first', 'missing', 'last']
    assert payload['topics'][1]['status'] == 'not_completed'
    assert '未重新检索' in payload['topics'][1]['summary']
    assert payload['recovery']['missing_topics'] == ['missing']
    assert payload['recovery']['expected_topics_known'] is True
    assert payload['recovery']['network_used'] is False
    assert payload['topics'][0]['checkpoint_generated_at'] == json.loads(first.read_text())['generated_at']
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_legacy_batch_recovery_does_not_claim_completeness(tmp_path):
    checkpoint(tmp_path, 'Fund')
    payload = load_report_archive(assemble_checkpoint_archive(tmp_path, tmp_path / 'out'))
    assert payload['recovery']['expected_topics_known'] is False
    assert payload['recovery']['saved_topic_count'] == 1
    assert payload['title'] == '研究（检查点恢复）'


@pytest.mark.parametrize('change', [
    {'settings': {'different': True}}, {'title': 'different title'},
    {'topics': [{'topic': 'two', 'summary': 'x'}, {'topic': 'extra', 'summary': 'x'}]},
])
def test_mixed_or_malformed_checkpoint_batch_is_rejected_before_writes(tmp_path, change):
    checkpoint(tmp_path, 'one')
    checkpoint(tmp_path, 'two', **change)
    output = tmp_path / 'out'
    with pytest.raises(ValueError):
        assemble_checkpoint_archive(tmp_path, output)
    assert not output.exists()


def test_wrong_checkpoint_filename_is_rejected(tmp_path):
    checkpoint(tmp_path, 'one').rename(tmp_path / 'topic_wrong.json')
    with pytest.raises(ValueError, match='identity'):
        assemble_checkpoint_archive(tmp_path)


@pytest.mark.parametrize('topics', [['one', 'one'], ['other'], [None], [], 'one'])
def test_manifest_topic_list_must_match_batch(tmp_path, topics):
    checkpoint(tmp_path, 'one')
    save_batch_manifest(tmp_path, '研究', topics)
    with pytest.raises(ValueError):
        assemble_checkpoint_archive(tmp_path)


def test_run_writes_manifest_before_first_topic_starts(tmp_path, monkeypatch):
    def process(topic):
        manifest = next(tmp_path.glob('.checkpoints/*/batch.json'))
        assert json.loads(manifest.read_text())['topics'] == ['one', 'two']
        return topic, 'summary'
    tracker = SimpleNamespace(config=Config(), process_topic=process)
    monkeypatch.setattr('core.orchestrator.ReportGenerator.create_pdf', lambda *a, **kw: None)
    Orchestrator(tracker).run(['one', ' two ', 'one'], str(tmp_path), 'Capital', '研究')


def test_recovery_cli_never_constructs_research_services(tmp_path, monkeypatch):
    checkpoint(tmp_path, 'Fund')
    forbidden = Mock(side_effect=AssertionError('research called'))
    monkeypatch.setattr(run, 'run_tracker', forbidden)
    monkeypatch.setattr(run, 'SearchService', forbidden)
    monkeypatch.setattr('core.report_generator.ReportGenerator.create_pdf', lambda *a, **kw: None)
    monkeypatch.setattr('sys.argv', ['run.py', '--recover-checkpoints', str(tmp_path), '--output-dir', str(tmp_path / 'out')])
    run.main()
    forbidden.assert_not_called()
    assert len(list((tmp_path / 'out').glob('*.json'))) == 2
