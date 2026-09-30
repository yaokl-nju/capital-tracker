import asyncio
import json
from types import SimpleNamespace

import pytest

from config.settings import Config
from core.orchestrator import Orchestrator


def test_checkpoint_saved_before_slow_topic_completes(tmp_path):
    cfg = Config()
    async def scenario():
        slow_started = asyncio.Event()
        release = asyncio.Event()
        checkpointed = asyncio.Event()
        async def process(topic):
            if topic == 'slow':
                slow_started.set()
                await release.wait()
            return topic, 'summary ' + topic
        def save(topic, summary):
            (tmp_path / f'{topic}.txt').write_text(summary)
            if topic == 'fast':
                loop.call_soon_threadsafe(checkpointed.set)
        loop = asyncio.get_running_loop()
        tracker = SimpleNamespace(config=cfg, process_topic_async=process)
        task = asyncio.create_task(Orchestrator(tracker).run_topics_async(['slow', 'fast'], 2, on_topic=save))
        await asyncio.wait_for(checkpointed.wait(), 1)
        assert slow_started.is_set() and not task.done()
        assert (tmp_path / 'fast.txt').read_text() == 'summary fast'
        assert not (tmp_path / 'slow.txt').exists()
        release.set()
        assert list((await task)) == ['slow', 'fast']
    asyncio.run(scenario())


def test_full_run_keeps_checkpoint_and_final_archives(tmp_path, monkeypatch):
    cfg = Config()
    tracker = SimpleNamespace(config=cfg, process_topic=lambda t: (t, 'summary ' + t))
    monkeypatch.setattr('core.orchestrator.ReportGenerator.create_pdf', lambda *a, **kw: None)
    output = Orchestrator(tracker).run(['Fund / A', 'Fund / B'], str(tmp_path), 'Capital', 'Capital')
    checkpoints = list((tmp_path / '.checkpoints').glob('*/topic_*.json'))
    assert len(checkpoints) == 2
    assert {json.loads(p.read_text())['topics'][0]['topic'] for p in checkpoints} == {'Fund / A', 'Fund / B'}
    assert all('单主题检查点' in json.loads(p.read_text())['title'] for p in checkpoints)
    assert all(json.loads(p.read_text())['report_status']['pdf'] == 'not_requested' for p in checkpoints)
    assert not list((tmp_path / '.checkpoints').rglob('*.pdf'))
    assert len(json.loads(open(output.replace('.pdf', '.json')).read())['topics']) == 2


def test_checkpoint_failure_does_not_lose_topic_results(capsys):
    tracker = SimpleNamespace(config=Config(), process_topic=lambda t: (t, 'summary'))
    def fail(*args):
        raise OSError('secret internal detail')
    result = asyncio.run(Orchestrator(tracker).run_topics_async(['Fund'], on_topic=fail))
    assert result == {'Fund': 'summary'}
    output = capsys.readouterr().out
    assert '检查点保存失败: OSError' in output and 'secret' not in output


def test_disabled_checkpoints_leave_only_final_archive(tmp_path, monkeypatch):
    cfg = Config()
    cfg.ENABLE_TOPIC_CHECKPOINTS = False
    tracker = SimpleNamespace(config=cfg, process_topic=lambda t: (t, 'summary'))
    monkeypatch.setattr('core.orchestrator.ReportGenerator.create_pdf', lambda *a, **kw: None)
    Orchestrator(tracker).run(['Fund'], str(tmp_path), 'Capital', 'Capital')
    assert not (tmp_path / '.checkpoints').exists()


def test_cancellation_preserves_completed_topics_only(tmp_path):
    async def scenario():
        checkpointed = asyncio.Event()
        async def process(topic):
            if topic == 'slow':
                await asyncio.Event().wait()
            return topic, 'summary'
        loop = asyncio.get_running_loop()
        def save(topic, summary):
            (tmp_path / f'{topic}.txt').write_text(summary)
            loop.call_soon_threadsafe(checkpointed.set)
        tracker = SimpleNamespace(config=Config(), process_topic_async=process)
        task = asyncio.create_task(Orchestrator(tracker).run_topics_async(['fast', 'slow'], on_topic=save))
        await asyncio.wait_for(checkpointed.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (tmp_path / 'fast.txt').exists() and not (tmp_path / 'slow.txt').exists()
    asyncio.run(scenario())
