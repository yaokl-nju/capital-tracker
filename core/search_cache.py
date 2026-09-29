"""Optional bounded SQLite cache; only public search results, never credentials."""
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import date
from pathlib import Path


def cache_identity(config, query, names):
    policy = {
        'schema': 1, 'query': query, 'names': names, 'day': date.today().isoformat(),
        'backends': config.SEARCH_BACKENDS,
        'region': config.SEARCH_REGION, 'country': config.SEARCH_GL,
        'language': config.SEARCH_HL, 'safesearch': config.SEARCH_SAFESEARCH,
        'allow': sorted(config.SOURCE_ALLOWLIST), 'deny': sorted(config.SOURCE_DENYLIST),
        'keys_present': [bool(getattr(config, name, '')) for name in
                         ('BRAVE_API_KEY', 'PARALLEL_SEARCH_API_KEY', 'SERPER_API_KEY', 'TAVILY_API_KEY')],
    }
    return hashlib.sha256(json.dumps(policy, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class SQLiteCache:
    """Separate short-lived connections allow safe use from search worker threads."""
    APPLICATION_ID = 1179209283  # FINC

    def __init__(self, path, max_size=500, ttl=3600):
        self.path, self.max_size, self.ttl = Path(path).expanduser(), max_size, ttl
        self.disabled = False
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError('Cache path must not be a symlink')
        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(handle)
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            application_id = db.execute('PRAGMA application_id').fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if application_id not in (0, self.APPLICATION_ID) or (application_id == 0 and tables):
                raise ValueError('Not a search cache database')
            db.execute(f'PRAGMA application_id = {self.APPLICATION_ID}')
            db.execute('CREATE TABLE IF NOT EXISTS search_cache '
                       '(key TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL, touched REAL NOT NULL)')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=1)

    @staticmethod
    def _key(query, timelimit, max_results):
        return hashlib.sha256(json.dumps([query, timelimit, max_results], sort_keys=True).encode()).hexdigest()

    def _disable(self, error):
        self.disabled = True
        print(f' ⚠️ 磁盘搜索缓存不可用，继续正常搜索: {type(error).__name__}')

    def get(self, query, timelimit, max_results=10):
        if self.disabled or self.ttl <= 0:
            return None
        key, now = self._key(query, timelimit, max_results), time.time()
        try:
            with closing(self._connect()) as db, db:
                row = db.execute('SELECT payload, created FROM search_cache WHERE key=?', (key,)).fetchone()
                if row is None:
                    return None
                if not isinstance(row[1], (int, float)) or not 0 <= now - row[1] < self.ttl:
                    db.execute('DELETE FROM search_cache WHERE key=?', (key,))
                    return None
                try:
                    results = json.loads(row[0])
                    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
                        raise ValueError('Invalid cached records')
                except (ValueError, TypeError):
                    db.execute('DELETE FROM search_cache WHERE key=?', (key,))
                    return None
                db.execute('UPDATE search_cache SET touched=? WHERE key=?', (now, key))
                return results
        except (sqlite3.Error, OSError) as error:
            self._disable(error)
            return None

    def set(self, query, timelimit, results, max_results=10):
        if self.disabled or self.ttl <= 0 or self.max_size <= 0 or not results:
            return
        key, now = self._key(query, timelimit, max_results), time.time()
        try:
            payload = json.dumps(results, ensure_ascii=False)
            with closing(self._connect()) as db, db:
                db.execute('INSERT INTO search_cache VALUES (?, ?, ?, ?) '
                           'ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, '
                           'created=excluded.created, touched=excluded.touched', (key, payload, now, now))
                db.execute('DELETE FROM search_cache WHERE created<=? OR created>?', (now - self.ttl, now))
                db.execute('DELETE FROM search_cache WHERE key IN '
                           '(SELECT key FROM search_cache ORDER BY touched DESC, key DESC LIMIT -1 OFFSET ?)',
                           (self.max_size,))
        except (sqlite3.Error, OSError, TypeError, ValueError) as error:
            self._disable(error)

    def clear(self):
        if not self.disabled:
            try:
                with closing(self._connect()) as db, db:
                    db.execute('DELETE FROM search_cache')
            except (sqlite3.Error, OSError) as error:
                self._disable(error)
