"""Brave quota cooldown never retries the failed request or consumes SEC capacity."""
from unittest.mock import Mock

import pytest
import requests

from config.settings import Config
from core.search_service import SearchService, SearchRequestGate


@pytest.mark.parametrize('headers,seconds', [
    ({'X-RateLimit-Remaining': '0, 1000', 'X-RateLimit-Reset': '1, 1419704'}, 1),
    ({'X-RateLimit-Remaining': '0, 0', 'X-RateLimit-Reset': '1, 1419704'}, 1419704),
    ({'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '0'}, 1),
    ({'X-RateLimit-Remaining': 'bad', 'X-RateLimit-Reset': 'NaN'}, 60),
    ({'Retry-After': '30'}, 30),
    ({'Retry-After': 'Infinity'}, 60),
    ({'Retry-After': '-1'}, 60),
    ({'X-RateLimit-Remaining': '0,0', 'X-RateLimit-Reset': '1', 'Retry-After': '5'}, 5),
])
def test_brave_cooldown_uses_exhausted_windows_only(headers, seconds):
    assert SearchService._brave_reset_seconds(headers) == seconds


def test_brave_429_is_shared_for_same_key_and_falls_back_without_retry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('core.search_service.time.monotonic', lambda: now[0])
    monkeypatch.setattr('core.search_service._BRAVE_COOLDOWNS', {})
    monkeypatch.setattr('core.search_service._SEARCH_REQUEST_GATE', SearchRequestGate())
    cfg = Config()
    cfg.BRAVE_API_KEY = 'test-private-quota-key'
    cfg.SEARCH_BACKENDS = ['brave', 'ddgs_text']
    cfg.SEARCH_MIN_INTERVALS = {}
    cfg.ENABLE_SEARCH_CACHE = False
    response = Mock(status_code=429, headers={'X-RateLimit-Remaining': '0, 0', 'X-RateLimit-Reset': '1, 120'})
    response.raise_for_status.side_effect = requests.HTTPError('quota', response=response)
    get = Mock(return_value=response)
    monkeypatch.setattr('core.search_service.requests.get', get)
    fallback = [{'title': 'BlackRock capital news', 'href': 'https://example.com/disclosure'}]
    first, second = SearchService(cfg), SearchService(cfg)
    first._try_ddgs_text = second._try_ddgs_text = Mock(return_value=fallback)
    assert first.search('capital')[0]['_backend'] == 'ddgs_text'
    assert second.search('another query')[0]['_backend'] == 'ddgs_text'
    assert get.call_count == 1
    now[0] += 121
    response.raise_for_status.side_effect = None
    response.json.return_value = {'web': {'results': []}}
    second.search('after reset')
    assert get.call_count == 2
    cfg.BRAVE_API_KEY = 'different-account-key'
    other = SearchService(cfg)
    other._try_ddgs_text = Mock(return_value=fallback)
    other.search('different account')
    assert get.call_count == 3


def test_cooling_down_request_uses_neither_general_nor_sec_gate(monkeypatch):
    import time
    cfg = Config()
    cfg.BRAVE_API_KEY = 'cooldown-key'
    cfg.SEARCH_MIN_INTERVALS = {}
    monkeypatch.setattr('core.search_service._BRAVE_COOLDOWNS', {cfg.BRAVE_API_KEY: time.monotonic() + 60})
    general, sec = Mock(), Mock()
    monkeypatch.setattr('core.search_service._SEARCH_REQUEST_GATE', general)
    monkeypatch.setattr('core.search_service._SEC_REQUEST_GATE', sec)
    assert SearchService(cfg)._run_backend('brave', 'q', 3, 'w') is None
    general.request.assert_not_called()
    sec.request.assert_not_called()
