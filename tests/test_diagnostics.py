import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from config.settings import Config, LLMConfig
from core.diagnostics import Diagnostics, capture_diagnostics, record_event
from core.search_service import SearchService, SearchAggregator
from core.tracker import Tracker
from core.llm_service import LLMService
from trackers.investment import InvestmentQueryGenerator, InvestmentSummarizer


def test_diagnostics_whitelist_and_bound():
    capture = Diagnostics(limit=2)
    for i in range(4):
        capture.append('backend', {'backend': 'brave', 'result_count': i,
                                   'api_key': 'secret', 'headers': {'Authorization': 'secret'},
                                   'prompt': 'private', 'outcome': []})
    snapshot = capture.snapshot()
    assert snapshot['dropped_events'] == 2 and len(snapshot['events']) == 2
    assert 'secret' not in json.dumps(snapshot) and 'private' not in json.dumps(snapshot)
    snapshot['events'][0]['backend'] = 'changed'
    assert capture.snapshot()['events'][0]['backend'] == 'brave'


def test_async_context_propagates_to_thread_and_topics_stay_isolated():
    async def topic(name):
        with capture_diagnostics() as capture:
            await asyncio.to_thread(record_event, 'backend', backend=name)
            await asyncio.sleep(0)
            record_event('stage', stage=name)
            return capture.snapshot()
    async def scenario():
        return await asyncio.gather(topic('A'), topic('B'))
    a, b = asyncio.run(scenario())
    assert a['events'] == [{'kind': 'backend', 'backend': 'A'}, {'kind': 'stage', 'stage': 'A'}]
    assert b['events'] == [{'kind': 'backend', 'backend': 'B'}, {'kind': 'stage', 'stage': 'B'}]
    # Outside an active topic, recording is a no-op.
    record_event('backend', backend='outside')


def test_threaded_observations_are_not_lost():
    capture = Diagnostics()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: capture.append('query', {'result_count': i}), range(200)))
    assert len(capture.snapshot()['events']) == 200


def test_search_observations_distinguish_empty_unavailable_and_cache(monkeypatch):
    cfg = Config()
    cfg.SEARCH_BACKENDS = ['ddgs_news', 'ddgs_text']
    cfg.SEARCH_CACHE_PATH = ''
    service = SearchService(cfg)
    monkeypatch.setattr(service, '_try_ddgs_news', lambda *a: None)
    monkeypatch.setattr(service, '_try_ddgs_text', lambda *a: [])
    with capture_diagnostics() as trace:
        assert service.search('q') == []
    assert [(e['backend'], e['outcome']) for e in trace.snapshot()['events'] if e['kind'] == 'backend'] == [
        ('ddgs_news', 'unavailable'), ('ddgs_text', 'empty')]
    assert trace.snapshot()['events'][-1]['outcome'] == 'empty'
    monkeypatch.setattr(service, '_try_ddgs_text', lambda *a: [{
        'href': 'https://example.com/event', 'title': 'Fund investment', 'body': 'USD 10 million',
        'source': 'Example', 'date': ''}])
    service.search('q')
    with capture_diagnostics() as trace:
        assert service.search('q')
    assert trace.snapshot()['events'][0]['kind'] == 'cache'
    assert all(e['kind'] != 'backend' for e in trace.snapshot()['events'])


def test_topic_archive_records_stage_and_search_events_with_no_model(monkeypatch):
    cfg = Config()
    cfg.SEARCH_BACKENDS = ['ddgs_text']
    cfg.SEARCH_CACHE_PATH = ''
    cfg.SEARCH_DELAY_RANGE = (0, 0)
    cfg.MAX_QUERIES = 1
    service = SearchService(cfg)
    monkeypatch.setattr(service, '_try_ddgs_text', lambda *a: [])
    tracker = Tracker(LLMService(LLMConfig('', '', 'model')), SearchAggregator(service),
                      InvestmentQueryGenerator(), InvestmentSummarizer(), cfg)
    asyncio.run(tracker.process_topic_async('Fund'))
    record = tracker.snapshot_records()['Fund']
    events = record['diagnostics']['events']
    assert [e['stage'] for e in events if e['kind'] == 'stage'] == ['query_generation', 'search', 'analysis']
    assert any(e['kind'] == 'model' and e['outcome'] == 'not_configured' for e in events)
    assert record['status'] == 'no_evidence'


def test_model_exception_records_type_without_message_or_headers():
    cfg = LLMConfig('', '', 'model')
    service = LLMService(cfg)
    service.client = Mock()
    service.client.chat.completions.create.side_effect = RuntimeError('api_key=secret')
    with capture_diagnostics() as trace:
        assert service.complete('private prompt') is None
    text = json.dumps(trace.snapshot())
    assert 'RuntimeError' in text and 'secret' not in text and 'private prompt' not in text


def test_http_rate_limit_records_status_without_response_credentials(monkeypatch):
    import requests
    from types import SimpleNamespace
    config = Config()
    service = SearchService(config)
    response = SimpleNamespace(status_code=429, headers={'Retry-After': '1', 'Authorization': 'secret-header'})
    error = requests.HTTPError('https://example.com?api_key=secret-query', response=response)
    monkeypatch.setattr('core.search_service.requests.post', Mock(side_effect=error))
    with capture_diagnostics() as trace:
        assert service._request_json('Parallel', 'post', 'https://example.com', headers={'x-api-key': 'secret-key'}) is None
    events = trace.snapshot()['events']
    assert events[0]['kind'] == 'request'
    assert events[0]['status_code'] == 429 and events[0]['error_type'] == 'HTTPError'
    assert 'secret' not in json.dumps(events) and 'example.com' not in json.dumps(events)


def test_model_token_usage_is_numeric_and_optional():
    from types import SimpleNamespace
    service = LLMService(LLMConfig('', '', 'model'))
    service.client = Mock()
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='analysis'))],
                               usage=SimpleNamespace(prompt_tokens=123, completion_tokens=56, total_tokens=179,
                                                     private_key='secret'))
    service.client.chat.completions.create.return_value = response
    with capture_diagnostics() as trace:
        assert service.complete('private prompt') == 'analysis'
    event = trace.snapshot()['events'][0]
    assert event['prompt_tokens'] == 123 and event['completion_tokens'] == 56 and event['total_tokens'] == 179
    assert 'secret' not in json.dumps(event) and 'private prompt' not in json.dumps(event)
    response.usage = SimpleNamespace(prompt_tokens=None, completion_tokens=True, total_tokens='179')
    with capture_diagnostics() as trace:
        service.complete('private prompt')
    assert not any(name.endswith('_tokens') for name in trace.snapshot()['events'][0])
