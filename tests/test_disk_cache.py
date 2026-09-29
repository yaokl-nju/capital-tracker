import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from config.settings import Config
from core.search_cache import SQLiteCache, cache_identity
from core.search_service import SearchService


def item():
    return {'title': 'BlackRock capital update', 'href': 'https://reuters.com/capital', 'body': 'USD 10 million'}


def test_disk_cache_survives_recreation_is_private_and_returns_independent_copies(tmp_path):
    path = tmp_path / 'cache.sqlite'
    cache = SQLiteCache(path)
    cache.set('query', 'w', [item()], 3)
    second = SQLiteCache(path)
    found = second.get('query', 'w', 3)
    found[0]['body'] = 'mutated'
    assert second.get('query', 'w', 3)[0]['body'] == 'USD 10 million'
    assert second.get('query', 'd', 3) is None and second.get('query', 'w', 4) is None
    assert path.stat().st_mode & 0o777 == 0o600
    second.clear()
    assert cache.get('query', 'w', 3) is None


def test_expiry_clock_rollback_and_empty_results(monkeypatch, tmp_path):
    now = [100.0]
    monkeypatch.setattr('core.search_cache.time.time', lambda: now[0])
    cache = SQLiteCache(tmp_path / 'cache.sqlite', ttl=10)
    cache.set('q', 'w', [item()])
    now[0] = 110
    assert cache.get('q', 'w') is None
    cache.set('q', 'w', [item()])
    now[0] = 109
    assert cache.get('q', 'w') is None
    cache.set('empty', 'w', [])
    assert cache.get('empty', 'w') is None


def test_lru_eviction_and_multiple_worker_connections(tmp_path):
    path = tmp_path / 'cache.sqlite'
    caches = [SQLiteCache(path, max_size=7), SQLiteCache(path, max_size=7)]
    def write(index):
        cache = caches[index % 2]
        cache.set(str(index), 'w', [item()])
        return cache.get(str(index), 'w')
    with ThreadPoolExecutor(8) as executor:
        list(executor.map(write, range(40)))
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM search_cache').fetchone()[0] <= 7
    assert not any(cache.disabled for cache in caches)


def test_corrupted_row_is_a_miss_and_recovers_without_network_errors(tmp_path):
    cache = SQLiteCache(tmp_path / 'cache.sqlite')
    cache.set('q', 'w', [item()])
    with sqlite3.connect(cache.path) as db:
        db.execute('UPDATE search_cache SET payload=?', ('{"not":"records"}',))
    assert cache.get('q', 'w') is None and not cache.disabled
    cache.set('q', 'w', [item()])
    assert cache.get('q', 'w')


@pytest.mark.parametrize('created', ['not-a-timestamp', b'broken', None, float('inf')])
def test_corrupt_cache_timestamps_are_misses_not_search_failures(tmp_path, created):
    cache = SQLiteCache(tmp_path / 'cache.sqlite')
    cache.set('q', 'w', [item()])
    with sqlite3.connect(cache.path) as db:
        # NULL is forbidden by the schema; simulate an older permissive cache table.
        if created is None:
            db.execute('ALTER TABLE search_cache RENAME TO old_cache')
            db.execute('CREATE TABLE search_cache (key TEXT PRIMARY KEY, payload TEXT, created REAL, touched REAL)')
            db.execute('INSERT INTO search_cache SELECT * FROM old_cache')
        db.execute('UPDATE search_cache SET created=?', (created,))
    assert cache.get('q', 'w') is None and not cache.disabled
    cache.set('q', 'w', [item()])
    assert cache.get('q', 'w')


def test_unrelated_database_is_not_modified(tmp_path):
    path = tmp_path / 'other.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE important (value TEXT)')
        db.execute('INSERT INTO important VALUES (?)', ('original',))
    with pytest.raises(ValueError):
        SQLiteCache(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT value FROM important').fetchone()[0] == 'original'
        assert db.execute('PRAGMA application_id').fetchone()[0] == 0
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='search_cache'").fetchall()


def test_service_shared_cache_and_no_cache_bypass(tmp_path):
    cfg = Config()
    cfg.SEARCH_CACHE_PATH = str(tmp_path / 'cache.sqlite')
    cfg.SEARCH_BACKENDS = ['ddgs_news']
    first = SearchService(cfg)
    first._run_backend = Mock(return_value=[item()])
    assert first.search('capital', required_names=['BlackRock'])
    second = SearchService(cfg)
    second._run_backend = Mock(side_effect=AssertionError('cache miss'))
    assert second.search('capital', required_names=['BlackRock'])
    second._run_backend.assert_not_called()
    cfg.ENABLE_SEARCH_CACHE = False
    third = SearchService(cfg)
    third._run_backend = Mock(return_value=[item()])
    assert third.search('capital', required_names=['BlackRock'])
    third._run_backend.assert_called_once()


def test_invalid_cache_path_falls_back_without_stopping_search(tmp_path):
    path = tmp_path / 'cache.sqlite'
    path.write_bytes(b'not a database')
    cfg = Config()
    cfg.SEARCH_CACHE_PATH = str(path)
    cfg.SEARCH_BACKENDS = ['ddgs_news']
    service = SearchService(cfg)
    service._run_backend = Mock(return_value=[item()])
    assert service.search('capital')
    assert path.read_bytes() == b'not a database'


def test_cache_policy_covers_locales_filters_backend_order_and_credentials_presence():
    cfg = Config()
    key = cache_identity(cfg, 'capital', ['BlackRock'])
    cfg.SEARCH_GL = 'CN'
    assert cache_identity(cfg, 'capital', ['BlackRock']) != key
    cfg.SEARCH_GL = 'US'
    cfg.SOURCE_DENYLIST = {'reuters.com'}
    assert cache_identity(cfg, 'capital', ['BlackRock']) != key
    cfg.SOURCE_DENYLIST = Config.SOURCE_DENYLIST
    cfg.SEARCH_BACKENDS = ['ddgs_text']
    assert cache_identity(cfg, 'capital', ['BlackRock']) != key


def test_cache_identity_changes_at_midnight_without_exposing_secrets(monkeypatch):
    from datetime import date
    cfg = Config()
    cfg.BRAVE_API_KEY = 'private-cache-test-key'
    class Today:
        @staticmethod
        def today():
            return date(2026, 9, 28)
    monkeypatch.setattr('core.search_cache.date', Today)
    before = cache_identity(cfg, 'capital', ['BlackRock'])
    Today.today = staticmethod(lambda: date(2026, 9, 29))
    after = cache_identity(cfg, 'capital', ['BlackRock'])
    assert before != after and 'private-cache-test-key' not in before + after


def test_cache_revalidates_untrusted_stored_urls_before_returning(tmp_path):
    cfg = Config()
    cfg.SEARCH_CACHE_PATH = str(tmp_path / 'cache.sqlite')
    cfg.SEARCH_BACKENDS = ['ddgs_news']
    service = SearchService(cfg)
    service.cache.set(cache_identity(cfg, 'capital', ()), 'w',
                      [{'title': 'bad', 'href': 'file:///private/data'}])
    service._run_backend = Mock(return_value=[item()])
    assert service.search('capital')[0]['href'] == 'https://reuters.com/capital'
    service._run_backend.assert_called_once()


def test_doctor_does_not_create_configured_disk_cache(monkeypatch, capsys, tmp_path):
    import run
    cfg = Config()
    cfg.SEARCH_CACHE_PATH = str(tmp_path / 'not-created' / 'cache.sqlite')
    run.show_readiness(cfg)
    assert json.loads(capsys.readouterr().out)['disk_cache_configured'] is True
    assert not Path(cfg.SEARCH_CACHE_PATH).exists()


def test_cache_is_reused_by_a_separate_python_process(tmp_path):
    path = tmp_path / 'cache.sqlite'
    cfg = Config()
    cfg.SEARCH_CACHE_PATH = str(path)
    cfg.SEARCH_BACKENDS = ['ddgs_news']
    service = SearchService(cfg)
    service._run_backend = Mock(return_value=[item()])
    assert service.search('capital', required_names=['BlackRock'])
    code = '''
import json, sys
from config.settings import Config
from core.search_service import SearchService
cfg = Config()
cfg.SEARCH_CACHE_PATH = sys.argv[1]
cfg.SEARCH_BACKENDS = ['ddgs_news']
service = SearchService(cfg)
def forbidden(*args):
    raise AssertionError('backend called instead of persisted cache')
service._run_backend = forbidden
print(json.dumps(service.search('capital', required_names=['BlackRock'])))
'''
    completed = subprocess.run([sys.executable, '-c', code, str(path)],
                               cwd=Path(__file__).resolve().parents[1], capture_output=True,
                               text=True, timeout=10, check=True)
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result[0]['href'] == 'https://reuters.com/capital'


def test_cache_io_error_after_startup_does_not_stop_service_search(tmp_path):
    cfg = Config()
    cfg.SEARCH_CACHE_PATH = str(tmp_path / 'cache.sqlite')
    cfg.SEARCH_BACKENDS = ['ddgs_news']
    service = SearchService(cfg)
    Path(cfg.SEARCH_CACHE_PATH).write_bytes(b'broken cache')
    service._run_backend = Mock(return_value=[item()])
    assert service.search('capital') and service.cache.disabled
