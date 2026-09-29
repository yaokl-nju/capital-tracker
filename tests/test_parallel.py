import asyncio
from datetime import date, timedelta
from unittest.mock import Mock

import pytest
import requests

from config.settings import Config
from core.search_service import SearchService, SearchAggregator


@pytest.fixture
def config():
    config = Config()
    config.PARALLEL_SEARCH_API_KEY = 'fake-parallel-secret'
    config.SEARCH_BACKENDS = ['parallel']
    config.SEARCH_MIN_INTERVALS = {}
    config.SEARCH_DELAY_RANGE = (0, 0)
    config.SEARCH_PROXY = 'http://localhost:7897'
    return config


def response(results):
    value = Mock()
    value.json.return_value = {'results': results}
    return value


@pytest.mark.parametrize('timelimit,days', [('d', 1), ('w', 7), ('m', 31), ('y', 366), (None, None)])
def test_parallel_ga_parameters_dates_excerpts_and_proxy(monkeypatch, config, timelimit, days):
    config.SEARCH_GL = 'CN'
    post = Mock(return_value=response([
        {'title': '资本披露', 'url': 'https://sec.gov/disclosure',
         'publish_date': date.today().isoformat(), 'excerpts': ['First fact', 'Second fact', None, 12]},
        {'title': '融资新闻', 'url': 'https://reuters.com/event', 'publish_date': None,
         'excerpts': ['Date is unknown']}, None, {'title': 'Bad', 'url': 'javascript:alert(1)'},
    ]))
    monkeypatch.setattr('core.search_service.requests.post', post)
    found = SearchService(config)._try_parallel('Fund investments', 99, timelimit)
    assert len(found) == 2 and found[0]['body'] == 'First fact\nSecond fact'
    assert found[0]['date'] == date.today().isoformat() and found[1]['date'] == ''
    assert found[0]['_score'] == 1.0
    post.assert_called_once()
    args = post.call_args
    assert args.args[0] == 'https://api.parallel.ai/v1/search'
    assert args.kwargs['headers']['x-api-key'] == config.PARALLEL_SEARCH_API_KEY
    assert args.kwargs['proxies'] == {'http': config.SEARCH_PROXY, 'https': config.SEARCH_PROXY}
    assert args.kwargs['timeout'] == config.SEARCH_TIMEOUT
    payload = args.kwargs['json']
    assert payload['search_queries'] == ['Fund investments'] and payload['mode'] == 'fast'
    assert config.PARALLEL_SEARCH_API_KEY not in str(payload)
    assert payload['advanced_settings'] == {
        'max_results': 20, 'excerpt_settings': {'max_chars_per_result': 4000}, 'location': 'cn'}
    assert 'after_date' not in str(payload)
    if days:
        assert (date.today() - timedelta(days=days)).isoformat() in payload['objective']
    else:
        assert 'published from' not in payload['objective']


def test_parallel_missing_key_skips_network(monkeypatch, config):
    config.PARALLEL_SEARCH_API_KEY = ''
    post = Mock(side_effect=AssertionError('network without key'))
    monkeypatch.setattr('core.search_service.requests.post', post)
    service = SearchService(config)
    assert service._enabled_backends() == []
    assert service._try_parallel('q', 3, 'w') is None
    post.assert_not_called()


@pytest.mark.parametrize('failure', [requests.Timeout(), requests.HTTPError(), ValueError('invalid JSON')])
def test_parallel_failure_once_and_falls_back_without_logging_secret(monkeypatch, config, capsys, failure):
    config.SEARCH_BACKENDS = ['parallel', 'serper']
    config.SERPER_API_KEY = 'fake-serper'
    post = Mock(side_effect=failure)
    monkeypatch.setattr('core.search_service.requests.post', post)
    service = SearchService(config)
    service._try_serper = Mock(return_value=[{'title': 'Fallback event', 'href': 'https://reuters.com/fallback'}])
    assert service.search('q')[0]['href'] == 'https://reuters.com/fallback'
    post.assert_called_once()
    service._try_serper.assert_called_once()
    assert config.PARALLEL_SEARCH_API_KEY not in capsys.readouterr().out


@pytest.mark.parametrize('data', [None, {}, {'results': None}, {'results': 'bad'}])
def test_parallel_malformed_response_is_failure(monkeypatch, config, data):
    value = Mock()
    value.json.return_value = data
    post = Mock(return_value=value)
    monkeypatch.setattr('core.search_service.requests.post', post)
    assert SearchService(config)._try_parallel('q', 3, 'w') is None
    post.assert_called_once()


def test_parallel_old_dates_filtered_unknown_dates_reach_async_analysis(monkeypatch, config):
    value = response([
        {'title': 'Old event', 'url': 'https://reuters.com/old',
         'publish_date': (date.today() - timedelta(days=30)).isoformat(), 'excerpts': ['USD 10 million']},
        {'title': 'Unknown event', 'url': 'https://sec.gov/new', 'publish_date': None,
         'excerpts': ['USD 20 million', 'Capital allocation']},
    ])
    post = Mock(return_value=value)
    monkeypatch.setattr('core.search_service.requests.post', post)
    service = SearchService(config)
    text = asyncio.run(SearchAggregator(service).aggregate_async(['fund']))
    assert 'reuters.com/old' not in text and 'sec.gov/new' in text
    assert '时效未核实' in text and 'USD 20 million' in text and 'Capital allocation' in text
    assert service.search('fund', config.MAX_RESULTS)  # Same limits hit the cache.
    post.assert_called_once()


def test_parallel_empty_success_and_local_domain_filter(monkeypatch, config):
    config.SOURCE_ALLOWLIST = {'sec.gov'}
    post = Mock(return_value=response([
        {'title': 'Blocked', 'url': 'https://example.com/x', 'excerpts': ['snippet']},
        {'title': 'Allowed', 'url': 'https://sec.gov/x', 'excerpts': ['snippet']},
    ]))
    monkeypatch.setattr('core.search_service.requests.post', post)
    service = SearchService(config)
    assert [item['href'] for item in service.search('q')] == ['https://sec.gov/x']
    post.return_value = response([])
    assert service.search('empty') == []
