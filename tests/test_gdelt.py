"""The optional keyless index must not promote discovery times to event dates."""
from unittest.mock import Mock

import pytest
import requests

from config.settings import Config
from core.search_service import SearchService, SearchAggregator, SearchRequestGate


def config():
    cfg = Config()
    cfg.SEARCH_BACKENDS = ['gdelt']
    cfg.SEARCH_PROXY = ''
    cfg.SEARCH_MIN_INTERVALS = {}
    cfg.ENABLE_SEARCH_CACHE = False
    return cfg


@pytest.mark.parametrize('window,timespan', [('d', '1day'), ('w', '1week'),
                                          ('m', '1month'), ('y', '3months'), (None, '3months')])
def test_gdelt_keyless_parameters_and_unknown_publication_date(monkeypatch, window, timespan):
    response = Mock()
    response.json.return_value = {'articles': [
        {'title': 'Capital financing news', 'url': 'https://reuters.com/capital',
         'seendate': '20260929T120000Z', 'domain': 'fake.gov'},
        None, {'title': 'Bad URL', 'url': 'file:///private/data'},
    ]}
    get = Mock(return_value=response)
    monkeypatch.setattr('core.search_service.requests.get', get)
    service = SearchService(config())
    results = service._try_gdelt('Capital financing', 999, window)
    params = get.call_args.kwargs['params']
    assert params['timespan'] == timespan and params['maxrecords'] == 250
    assert params['query'] == 'Capital financing' and params['format'] == 'json'
    assert 'headers' not in get.call_args.kwargs
    assert len(results) == 1 and results[0]['date'] == ''
    assert results[0]['source'] == 'reuters.com'
    assert '仅标题，未读取正文' in results[0]['body']
    text = SearchAggregator(service)._format_results(results, True)
    assert '时间: 未提供（时效未核实）' in text
    assert '20260929T120000Z' in text


@pytest.mark.parametrize('payload,expected', [({}, []), ({'articles': []}, []),
                                           ({'error': 'invalid query'}, None),
                                           ({'articles': {}}, None)])
def test_gdelt_empty_and_malformed_responses(monkeypatch, payload, expected):
    response = Mock()
    response.json.return_value = payload
    monkeypatch.setattr('core.search_service.requests.get', Mock(return_value=response))
    assert SearchService(config())._try_gdelt('capital', 3, 'w') == expected


def test_gdelt_http_failure_is_single_attempt_and_can_fall_back(monkeypatch):
    cfg = config()
    cfg.SEARCH_BACKENDS = ['gdelt', 'ddgs_text']
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError('rate limit')
    get = Mock(return_value=response)
    monkeypatch.setattr('core.search_service.requests.get', get)
    monkeypatch.setattr('core.search_service._GDELT_LAST_REQUEST', {})
    service = SearchService(cfg)
    service._try_ddgs_text = Mock(return_value=[{
        'title': 'Fallback capital news', 'href': 'https://example.com/news', 'body': 'USD 10 million',
    }])
    assert service.search('capital')[0]['title'] == 'Fallback capital news'
    assert get.call_count == 1 and service._try_ddgs_text.call_count == 1


def test_gdelt_spacing_is_shared_across_instances_and_cannot_be_disabled(monkeypatch):
    now, pauses, starts = [100.0], [], []
    monkeypatch.setattr('core.search_service.time.monotonic', lambda: now[0])
    def sleep(seconds):
        pauses.append(seconds)
        now[0] += seconds
    monkeypatch.setattr('core.search_service.time.sleep', sleep)
    monkeypatch.setattr('core.search_service._SEARCH_REQUEST_GATE', SearchRequestGate())
    monkeypatch.setattr('core.search_service._GDELT_LAST_REQUEST', {})
    services = [SearchService(config()), SearchService(config())]
    for service in services:
        service._try_gdelt = lambda *args: (starts.append(now[0]), [])[1]
        service._run_backend('gdelt', 'capital', 3, 'w')
    assert starts == [100.0, 105.0] and pauses == [1] * 5


def test_gdelt_is_available_without_keys_but_is_not_in_default_priority():
    cfg = Config()
    assert 'gdelt' not in cfg.SEARCH_BACKENDS
    cfg.SEARCH_BACKENDS = ['gdelt']
    assert SearchService(cfg)._enabled_backends() == ['gdelt']


def test_gdelt_429_cools_down_across_instances_without_retry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('core.search_service.time.monotonic', lambda: now[0])
    monkeypatch.setattr('core.search_service._SEARCH_REQUEST_GATE', SearchRequestGate())
    monkeypatch.setattr('core.search_service._GDELT_LAST_REQUEST', {})
    response = Mock(status_code=429, headers={'Retry-After': '30'})
    response.raise_for_status.side_effect = requests.HTTPError('rate limited', response=response)
    get = Mock(return_value=response)
    monkeypatch.setattr('core.search_service.requests.get', get)
    assert SearchService(config()).search('capital') is None
    assert SearchService(config()).search('another capital query') is None
    assert get.call_count == 1
    now[0] += 31
    response.raise_for_status.side_effect = None
    response.json.return_value = {'articles': []}
    assert SearchService(config()).search('capital') == []
    assert get.call_count == 2
