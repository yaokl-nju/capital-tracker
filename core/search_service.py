"""Financial search, evidence processing, independent limits and SEC disclosures."""
import time
import asyncio
from contextlib import contextmanager
import re
from copy import deepcopy
from threading import RLock, BoundedSemaphore
from html.parser import HTMLParser
from typing import List, Optional, Dict, Any, Set
from collections import OrderedDict, deque
from urllib.parse import quote, urlparse, parse_qsl, urlencode, urlunparse, urljoin
import difflib
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
try:
    from ddgs import DDGS
except ImportError:
    DDGS = None
import requests

from config.settings import Config
from config.fund_mappings import get_sec_edgar_name, get_sec_cik


class SearchRequestGate:
    """Limit one request pool to 10 starts/second and 10 in flight."""

    def __init__(self, limit=10):
        self.limit = limit
        self.slots = BoundedSemaphore(limit)
        self.lock = RLock()
        self.started = deque()

    @contextmanager
    def request(self):
        with self.slots:
            while True:
                with self.lock:
                    now = time.monotonic()
                    while self.started and now - self.started[0] >= 1:
                        self.started.popleft()
                    if len(self.started) < self.limit:
                        self.started.append(now)
                        break
                time.sleep(1)
            yield


# All general-search backends share one pool; SEC has an independent pool.
_SEARCH_REQUEST_GATE = SearchRequestGate()
_SEC_REQUEST_GATE = SearchRequestGate()


# Simple in-memory cache with TTL
class SimpleCache:
    """Simple LRU cache with Time-To-Live support."""

    def __init__(self, max_size: int = 500, ttl: int = 3600):
        """Initialize cache."""
        self.cache = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl
        self.lock = RLock()

    def _get_key(self, query, timelimit, max_results=10):
        return (query, timelimit, max_results)

    def get(self, query, timelimit, max_results=10):
        with self.lock:
            key = self._get_key(query, timelimit, max_results)
            entry = self.cache.get(key)
            if entry is None:
                return None
            results, timestamp = entry
            if time.monotonic() - timestamp >= self.ttl:
                del self.cache[key]
                return None
            self.cache.move_to_end(key)
            return deepcopy(results)

    def set(self, query, timelimit, results, max_results=10):
        with self.lock:
            if self.max_size <= 0 or self.ttl <= 0:
                return
            key = self._get_key(query, timelimit, max_results)
            self.cache[key] = (deepcopy(results), time.monotonic())
            self.cache.move_to_end(key)
            while len(self.cache) > self.max_size:
                self.cache.popitem(last=False)

    def clear(self):
        with self.lock:
            self.cache.clear()


def get_domain(url: str) -> str:
    """Extract domain from URL."""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def published_date(value: str):
    """Parse explicit publication dates and common relative search dates."""
    if not value:
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).date()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).date()
    except (ValueError, TypeError, OverflowError):
        pass
    for date_format in ('%b %d, %Y', '%B %d, %Y', '%Y年%m月%d日', '%Y/%m/%d'):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            pass
    if text.lower() in ('yesterday', '昨天'):
        return (datetime.now() - timedelta(days=1)).date()
    relative = re.fullmatch(r'(\d+)\s*(hour|day|week|month|year)s?\s+ago', text, re.I)
    if relative:
        count, unit = relative.groups()
        if len(count) > 6:
            return None
        days = int(count) * {'hour': 1 / 24, 'day': 1, 'week': 7, 'month': 30, 'year': 365}[unit.lower()]
        if days > 36600:
            return None
        return (datetime.now() - timedelta(days=days)).date()
    chinese_relative = re.fullmatch(r'(\d+)\s*(天|周|个月|年|小时|分钟)前', text)
    if chinese_relative:
        count, unit = chinese_relative.groups()
        if len(count) > 6:
            return None
        days = int(count) * {'天': 1, '周': 7, '个月': 30, '年': 365, '小时': 1 / 24, '分钟': 1 / 1440}[unit]
        if days <= 36600:
            return (datetime.now() - timedelta(days=days)).date()
    return None


def is_similar_title(title1: str, title2: str, threshold: float = 0.8) -> bool:
    """Check if two titles are similar using SequenceMatcher."""
    if not title1 or not title2:
        return False

    ratio = difflib.SequenceMatcher(None, title1.lower(), title2.lower()).ratio()
    return ratio >= threshold


def domain_matches(domain: str, rule: str) -> bool:
    """Match a hostname exactly or beneath a domain, never by substring."""
    rule = rule.lower().strip().strip('.')
    return bool(rule) and (domain == rule or domain.endswith('.' + rule))


def canonical_url(url: str) -> str:
    """Drop fragments and known tracking parameters; retain document identity."""
    try:
        if re.search(r'[\x00-\x20]', url):
            return ''
        parsed = urlparse(url.strip())
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return ''
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return ''
        params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                  if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or '/',
                           parsed.params, urlencode(sorted(params)), ''))
    except (ValueError, AttributeError):
        return ''


def calculate_source_score(source: str, domain: str, allowlist: Set[str], denylist: Set[str]) -> float:
    domain = domain.lower()
    if not domain:
        return 0.0
    for rule in denylist:
        if (rule.endswith('.') and domain.startswith(rule.lower())) or domain_matches(domain, rule):
            return 0.0
    if allowlist and not any(domain_matches(domain, rule) for rule in allowlist):
        return 0.0
    high_trust = {'reuters.com', 'apnews.com', 'bloomberg.com', 'wsj.com', 'ft.com',
                  'economist.com', 'nytimes.com', 'sec.gov',
                  'sse.com.cn', 'szse.cn', 'hkexnews.hk', 'hkex.com.hk', 'pbc.gov.cn',
                  'safe.gov.cn', 'csrc.gov.cn', 'cninfo.com.cn'}
    if any(domain_matches(domain, rule) for rule in high_trust):
        return 1.0
    medium_trust = {'cnbc.com', 'marketwatch.com', 'forbes.com', 'techcrunch.com', 'caixin.com',
                    'stcn.com', 'cs.com.cn', 'yicai.com', 'eastmoney.com'}
    if any(domain_matches(domain, rule) for rule in medium_trust):
        return 0.7
    return 0.5


class SearchService:
    """Multi-backend search service with automatic degradation and optimizations."""

    def __init__(self, config: Config = None):
        """Initialize search service."""
        self.config = config or Config()
        self._backend_locks = {name: RLock() for name in ("ddgs", "ddgs_news", "ddgs_text", "serper", "brave", "parallel", "tavily")}
        self._backend_last_request = {}
        self.cache = SimpleCache(
            max_size=500,
            ttl=self.config.SEARCH_CACHE_TTL if self.config.ENABLE_SEARCH_CACHE else 0
        )

    def search(
        self,
        query: str,
        max_results: int = 10,
        timelimit: str = 'w'
    ) -> Optional[List[Dict[str, Any]]]:
        """Search across configurable backends with automatic fallback."""
        query = query.strip() if isinstance(query, str) else ''
        if not query or max_results <= 0:
            return []
        if timelimit not in (None, '', 'd', 'w', 'm', 'y'):
            raise ValueError('Unsupported search time limit')
        # Check cache first
        if self.config.ENABLE_SEARCH_CACHE:
            cached = self.cache.get(query, timelimit, max_results)
            if cached:
                print(f"  [缓存命中] {query[:30]}...")
                return cached

        completed = False
        for backend in self._enabled_backends():
            results = self._run_backend(backend, query, max_results, timelimit)
            if results is not None:
                completed = True
            if results:
                results = self._process_results(results)
                if timelimit:
                    days = {'d': 1, 'w': 7, 'm': 31, 'y': 366}[timelimit]
                    cutoff = (datetime.now() - timedelta(days=days)).date()
                    results = [r for r in results if (date := published_date(r['date'])) is None or date >= cutoff]
                results = results[:max_results]
                if results:
                    if self.config.ENABLE_SEARCH_CACHE:
                        self.cache.set(query, timelimit, results, max_results)
                    return results

        return [] if completed else None

    def _run_backend(
        self,
        backend: str,
        query: str,
        max_results: int,
        timelimit: str
    ) -> Optional[List[Dict[str, Any]]]:
        """Dispatch a single search to the named backend method."""
        method = getattr(self, f"_try_{backend}", None)
        if method is None:
            print(f" ⚠️ 未知搜索后端: {backend}")
            return None
        try:
            interval = getattr(self.config, 'SEARCH_MIN_INTERVALS', {}).get(backend, 0)
            # Keep spacing tied to the actual request start, including gate waits.
            if interval > 0:
                with self._backend_locks[backend]:
                    previous = self._backend_last_request.get(backend)
                    while previous is not None and time.monotonic() - previous < interval:
                        time.sleep(1)
                    with _SEARCH_REQUEST_GATE.request():
                        self._backend_last_request[backend] = time.monotonic()
                        return method(query, max_results, timelimit)
            with _SEARCH_REQUEST_GATE.request():
                return method(query, max_results, timelimit)
        except Exception as exc:
            print(f" ⚠️ {backend} 搜索失败: {type(exc).__name__}")
            return None

    def _enabled_backends(self) -> List[str]:
        """Return backend priority order, skipping unavailable methods or missing keys."""
        # backend name -> config attribute holding the required API key
        key_attr = {
            'serper': 'SERPER_API_KEY',
            'tavily': 'TAVILY_API_KEY',
            'brave': 'BRAVE_API_KEY',
            'parallel': 'PARALLEL_SEARCH_API_KEY',
        }
        enabled = []
        for name in self.config.SEARCH_BACKENDS:
            if not getattr(self, f"_try_{name}", None):
                continue
            required = key_attr.get(name)
            if required and not getattr(self.config, required, ''):
                continue
            enabled.append(name)
        return enabled

    def _proxy_map(self) -> Optional[Dict[str, str]]:
        """Build a requests `proxies` dict from Config.SEARCH_PROXY (None = no proxy)."""
        proxy = getattr(self.config, 'SEARCH_PROXY', None)
        if not proxy:
            return None
        return {"http": proxy, "https": proxy}

    def _timeout(self) -> int:
        """Per-backend HTTP timeout (seconds)."""
        return int(getattr(self.config, 'SEARCH_TIMEOUT', 15) or 15)

    def _deduplicate_results(
        self,
        results: List[Dict[str, Any]],
        similarity_threshold: float = 0.85
    ) -> List[Dict[str, Any]]:
        """Deduplicate search results by URL and title similarity."""
        seen_urls = {}
        deduplicated = []

        for result in results:
            url, title = result['href'], result['title']
            if url in seen_urls:
                # Query-specific excerpts from one disclosure can carry different facts.
                previous = seen_urls[url]
                body = result.get('body', '').strip()
                if body and body not in previous.get('body', ''):
                    current_body = previous.get('body', '')
                    if current_body and current_body in body:
                        previous['body'] = body[:4000]
                    else:
                        extra = f"\n补充检索摘要（来源日期：{result.get('date') or '未提供'}）：\n{body}"
                        previous['body'] = (current_body + extra)[:4000]
                continue

            # Check title similarity
            is_duplicate = False
            for previous in deduplicated:
                current_date, previous_date = published_date(result.get('date', '')), published_date(previous.get('date', ''))
                if current_date and previous_date and current_date != previous_date:
                    continue
                if self._financial_fingerprint(result.get('body', '')) != self._financial_fingerprint(previous.get('body', '')):
                    continue
                if self._same_story(title, previous['title'], similarity_threshold):
                    is_duplicate = True
                    break

            if is_duplicate:
                continue

            # This result is unique
            seen_urls[url] = result
            deduplicated.append(result)

        return deduplicated

    @staticmethod
    def _financial_fingerprint(text):
        # Avoid merging different amounts, currencies and financing rounds.
        return re.findall(r'\d+(?:[.,]\d+)*|million|billion|trillion|usd|eur|cny|hkd|rmb|'
                          r'万元|亿元|万亿|人民币|美元|港元|欧元|series\s+[a-z]|[A-Za-z]轮', text.lower())

    @staticmethod
    def _same_story(title, previous, threshold=0.85):
        # Dates, quarters, values and percentages distinguish capital events.
        if SearchService._financial_fingerprint(title) != SearchService._financial_fingerprint(previous):
            return False
        changes = ('increase', 'decrease', 'buy', 'sell', '增持', '减持', '买入', '卖出')
        if any((word in title.lower()) != (word in previous.lower()) for word in changes):
            return False
        return is_similar_title(title, previous, threshold)

    def _request_json(self, backend, method, url, **kwargs):
        """One HTTP attempt with shared timeout/proxy and credential-safe errors."""
        try:
            response = getattr(requests, method)(
                url, timeout=self._timeout(), proxies=self._proxy_map(), **kwargs)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError('Invalid search response')
            return data
        except Exception as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', '未知')
            print(f' ⚠️ {backend} 单次搜索失败: {type(exc).__name__}（HTTP {status}）')
            return None

    def _map_results(self, items, url='url', body='body', date='date'):
        """Map provider fields into the common evidence schema."""
        if not isinstance(items, list):
            return None
        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = item.get(body) or ''
            if body == 'excerpts':
                if isinstance(text, list):
                    text = '\n'.join(part for part in text if isinstance(part, str))
                text = text[:4000] if isinstance(text, str) else ''
            results.append({'title': item.get('title', ''),
                            'href': item.get(url) or item.get('href', ''),
                            'body': text, 'date': item.get(date) or '',
                            'source': item.get('source') or ''})
        return self._process_results(results)

    def _try_ddgs_news(self, query, max_results, timelimit):
        return self._search_ddgs(query, max_results, timelimit, surface='news', backend='bing')

    def _try_ddgs_text(self, query, max_results, timelimit):
        return self._search_ddgs(query, max_results, timelimit, surface='text', backend='duckduckgo')

    def _try_ddgs(self, query, max_results, timelimit):
        """Keep the previous backend name as a text-search alias."""
        return self._try_ddgs_text(query, max_results, timelimit)

    def _search_ddgs(self, query, max_results, timelimit, surface, backend):
        """One DDGS surface/engine per attempt, sharing locale, proxy and filtering."""
        if DDGS is None:
            return None
        region = getattr(self.config, 'SEARCH_REGION', None) or 'wt-wt'
        if surface == 'news' and region == 'wt-wt':
            # Bing news requires a country/language pair, not DDG's worldwide code.
            region = f'{self.config.SEARCH_GL.lower()}-{self.config.SEARCH_HL.split("-")[0].lower()}'
        safesearch = getattr(self.config, 'SEARCH_SAFESEARCH', 'moderate') or 'moderate'
        proxy = getattr(self.config, 'SEARCH_PROXY', None) or None

        kwargs = dict(region=region, safesearch=safesearch, max_results=max_results)
        if timelimit:
            kwargs['timelimit'] = timelimit

        # Pin one engine to avoid hidden auto fan-out. News and text are
        # separate priorities, with other providers attempted between them.
        try:
            with DDGS(proxy=proxy, timeout=self._timeout()) as ddgs:
                results = getattr(ddgs, surface)(query, backend=backend, **kwargs)
                return self._map_results(results or [])
        except Exception as exc:
            if type(exc).__name__ == 'DDGSException' and str(exc) == 'No results found.':
                return []
            print(f' ⚠️ DDGS {surface} 单次搜索失败: {type(exc).__name__}')
            return None

    def _try_parallel(self, query, max_results, timelimit):
        """Parallel GA search, using fast mode and retaining undated evidence."""
        key = self.config.PARALLEL_SEARCH_API_KEY
        if not key:
            return None
        objective = (f'Find investment and financial capital evidence for: {query}. '
                     'Prefer original institutional disclosures and reliable financial reporting. ')
        if timelimit:
            today = datetime.now().date()
            cutoff = today - timedelta(days={'d': 1, 'w': 7, 'm': 31, 'y': 366}[timelimit])
            objective += (f'Prefer sources published from {cutoff.isoformat()} to {today.isoformat()} '
                          '(inclusive). Keep relevant undated evidence when dated evidence is scarce; '
                          'do not invent publication dates.')
        payload = {
            'search_queries': [query], 'objective': objective, 'mode': 'fast',
            'advanced_settings': {
                'max_results': min(max_results, 20),
                'excerpt_settings': {'max_chars_per_result': 4000},
                'location': self.config.SEARCH_GL.lower(),
            },
        }
        data = self._request_json('Parallel', 'post', 'https://api.parallel.ai/v1/search',
                                 headers={'x-api-key': key, 'Content-Type': 'application/json'}, json=payload)
        if data is None:
            return None
        return self._map_results(data.get('results'), body='excerpts', date='publish_date')

    def _try_serper(self, query, max_results, timelimit):
        """Google SERP through Serper, with configured locale and time window."""
        key = self.config.SERPER_API_KEY
        if not key:
            return None
        payload = {'q': query, 'gl': self.config.SEARCH_GL or 'US',
                   'hl': self.config.SEARCH_HL or 'en', 'num': max_results}
        if timelimit:
            payload['tbs'] = f'qdr:{timelimit}'
        data = self._request_json('Serper', 'post', 'https://google.serper.dev/search',
                                 headers={'X-API-KEY': key, 'Content-Type': 'application/json'}, json=payload)
        if data is None:
            return None
        return self._map_results(data.get('organic') or [], url='link', body='snippet')

    def _try_tavily(self, query, max_results, timelimit):
        """Tavily news search with dated content snippets."""
        key = self.config.TAVILY_API_KEY
        if not key:
            return None
        payload = {'query': query, 'max_results': min(max_results, 20), 'search_depth': 'basic',
                   'include_answer': False, 'include_raw_content': False,
                   'topic': 'news', 'include_published_date': True}
        if timelimit:
            payload['time_range'] = {'d': 'day', 'w': 'week', 'm': 'month', 'y': 'year'}[timelimit]
        data = self._request_json('Tavily', 'post', 'https://api.tavily.com/search',
                                 headers={'Authorization': f'Bearer {key}'}, json=payload)
        if data is None:
            return None
        return self._map_results(data.get('results') or [], body='content', date='published_date')

    def _try_brave(self, query, max_results, timelimit):
        """Brave web search with configured freshness, language and country."""
        key = self.config.BRAVE_API_KEY
        if not key:
            return None
        headers = {'Accept': 'application/json', 'Accept-Encoding': 'gzip', 'X-Subscription-Token': key}
        params = {'q': query, 'count': min(max_results, 20), 'country': self.config.SEARCH_GL,
                  'search_lang': self.config.SEARCH_HL.lower(), 'safesearch': self.config.SEARCH_SAFESEARCH}
        if timelimit:
            params['freshness'] = {'d': 'pd', 'w': 'pw', 'm': 'pm', 'y': 'py'}[timelimit]
        data = self._request_json('Brave', 'get', 'https://api.search.brave.com/res/v1/web/search',
                                 headers=headers, params=params)
        if data is None or not isinstance(data.get('web') or {}, dict):
            return None
        return self._map_results((data.get('web') or {}).get('results') or [], body='description', date='page_age')

    def _process_results(self, results):
        """Normalize, score and filter records once before financial-event deduplication."""
        normalized = []
        for result in results:
            if not isinstance(result, dict):
                continue
            url = canonical_url(result.get('href') or result.get('url') or '')
            title = result.get('title')
            if not url or not isinstance(title, str) or not title.strip():
                continue
            domain = get_domain(url)
            score = calculate_source_score(result.get('source', ''), domain,
                                           self.config.SOURCE_ALLOWLIST, self.config.SOURCE_DENYLIST)
            if score == 0:
                continue
            item = dict(result)
            item.update(href=url, title=title.strip()[:512],
                        body=str(result.get('body') or result.get('description') or ''),
                        source=str(result.get('source') or domain), date=str(result.get('date') or ''),
                        _domain=domain, _score=score)
            normalized.append(item)
        normalized.sort(key=lambda item: item['_score'], reverse=True)
        return self._deduplicate_results(normalized)


class _FilingTableParser(HTMLParser):
    """Keep EDGAR fields within the same row, including compact HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.cells = None
        self.cell = None
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.cells, self.links = [], []
        elif tag == 'td' and self.cells is not None:
            self.cell = []
        elif tag == 'a' and self.cells is not None:
            href = dict(attrs).get('href', '')
            if '/archives/edgar/data/' in href.lower():
                self.links.append(urljoin('https://www.sec.gov', href))

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == 'td' and self.cell is not None:
            self.cells.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.cells is not None:
            self.rows.append((self.cells, self.links))
            self.cells, self.cell, self.links = None, None, []


class SecEdgarSearcher:
    """Read SEC filing metadata; metadata alone cannot establish positions."""

    BASE_URL = 'https://www.sec.gov/cgi-bin/browse-edgar'

    def __init__(self, config: Config = None):
        self.config = config or Config()
        self.headers = {'User-Agent': self.config.SEC_USER_AGENT, 'Accept': 'text/html'}

    def _get(self, url, **kwargs):
        with _SEC_REQUEST_GATE.request():
            response = requests.get(
                url, headers=self.headers, timeout=self.config.SEARCH_TIMEOUT,
                proxies=({'http': self.config.SEARCH_PROXY, 'https': self.config.SEARCH_PROXY}
                         if self.config.SEARCH_PROXY else None), **kwargs)
            response.raise_for_status()
            return response

    @staticmethod
    def _normalize_form(form):
        return form.upper().replace('SCHEDULE 13G', 'SC 13G').replace('SCHEDULE 13D', 'SC 13D')

    def _search_submissions(self, cik, filing_types, max_results, days_back):
        """Use the official columnar JSON API for a verified or explicit CIK."""
        try:
            data = self._get(f'https://data.sec.gov/submissions/CIK{cik}.json').json()
            if 'cik' in data and int(data['cik']) != int(cik):
                raise ValueError('Unexpected filer CIK')
            recent = data['filings']['recent']
            fields = [recent[name] for name in ('form', 'filingDate', 'accessionNumber', 'primaryDocument')]
            if not all(isinstance(column, list) for column in fields):
                raise ValueError('Invalid submissions columns')
            if len({len(column) for column in fields}) != 1:
                raise ValueError('Misaligned submissions columns')
            cutoff = (datetime.now() - timedelta(days=days_back)).date()
            today = datetime.now().date()
            allowed = {self._normalize_form(t) for t in filing_types}
            allowed |= {t + '/A' for t in list(allowed) if not t.endswith('/A')}
            periods = recent.get('reportDate', [])
            results = []
            for index, (form, date_text, accession, document) in enumerate(zip(*fields)):
                if not all(isinstance(value, str) for value in (form, date_text, accession, document)):
                    continue
                if self._normalize_form(form) not in allowed:
                    continue
                try:
                    filed = datetime.strptime(date_text, '%Y-%m-%d').date()
                except ValueError:
                    continue
                if not cutoff <= filed <= today or not re.fullmatch(r'\d{10}-\d{2}-\d{6}', accession):
                    continue
                if not document or document.startswith('/') or '..' in document.split('/'):
                    continue
                url = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace("-", "")}/{quote(document, safe="/._-")}'
                period = periods[index] if isinstance(periods, list) and index < len(periods) else ''
                notice = '13F-NT 是通知文件，不是持仓明细。' if form.startswith('13F-NT') else ''
                results.append({
                    'title': f'{data.get("name", cik)} — {form} Filing — {date_text}',
                    'href': url, 'source': 'SEC.gov', 'date': date_text, '_score': 1.0,
                    '_filing_type': form,
                    'body': f'SEC {form} 申报元数据；注册主体 {data.get("name", cik)}，CIK {cik}；'
                            f'申报日期 {date_text}，报告期 {period or "未提供"}。{notice}'
                            '不含持仓明细。13F 是季度末快照，不代表实时交易；'
                            '没有持仓表和前后期对照，不能确认具体持仓或新建仓、增减持。',
                })
            results.sort(key=lambda item: item['date'], reverse=True)
            return results[:max_results]
        except Exception as exc:
            print(f' ⚠️ SEC submissions API 单次查询失败: {type(exc).__name__}')
            return []

    def search_recent_filings(self, company_name, filing_types=None, max_results=10, days_back=None):
        if not company_name.strip() or max_results <= 0:
            return []
        if not self.config.SEC_USER_AGENT:
            print('  [SEC] 未配置 SEC_USER_AGENT（应用名称及联系邮箱），使用通用搜索')
            return []
        types = filing_types or ['13F-HR', '13F-NT', 'SC 13G', 'SC 13D']
        aliases = {'13F': '13F-HR', '13G': 'SC 13G', '13D': 'SC 13D'}
        days_back = self.config.SEC_LOOKBACK_DAYS if days_back is None else days_back
        types = list(dict.fromkeys(aliases.get(t, t) for t in types))
        cik = get_sec_cik(company_name)
        if cik:
            return self._search_submissions(cik, types, max_results, days_back)
        results = []
        for filing_type in types:
            try:
                response = self._get(
                    self.BASE_URL,
                    params={'company': get_sec_edgar_name(company_name), 'type': filing_type,
                            'owner': 'exclude', 'count': str(min(max_results * 2, 100)),
                            'action': 'getcompany'})
                parsed = self._parse_general_filings(response.text, filing_type, days_back)
                for item in parsed:
                    item['title'] = f"{company_name} — {item['title']}"
                results.extend(parsed)
            except Exception as exc:
                print(f' ⚠️ SEC 搜索 {filing_type} 失败: {type(exc).__name__}')
        results.sort(key=lambda item: item['date'], reverse=True)
        seen = set()
        unique = []
        for item in results:
            if item['href'] not in seen:
                seen.add(item['href'])
                unique.append(item)
        return unique[:max_results]

    def _parse_general_filings(self, html, filing_type, days_back=None):
        parser = _FilingTableParser()
        parser.feed(html)
        cutoff = (datetime.now() - timedelta(days=(self.config.SEC_LOOKBACK_DAYS
                  if days_back is None else days_back))).date()
        today = datetime.now().date()
        results = []
        for cells, links in parser.rows:
            if not cells or not links:
                continue
            actual_type = cells[0].upper()
            if self._normalize_form(actual_type) not in (self._normalize_form(filing_type), self._normalize_form(filing_type) + '/A'):
                continue
            date_text = next((c for c in cells if re.fullmatch(r'\d{4}-\d{2}-\d{2}', c)), '')
            try:
                filing_date = datetime.strptime(date_text, '%Y-%m-%d').date()
            except ValueError:
                continue
            if not cutoff <= filing_date <= today:
                continue
            link = canonical_url(links[0])
            if not domain_matches(get_domain(link), 'sec.gov'):
                continue
            description = cells[2] if len(cells) > 2 else ''
            if actual_type.startswith('13F-NT'):
                description += ' 13F-NT 是通知文件，不是持仓明细。'
            results.append({
                'title': f'{actual_type} Filing — {date_text}', 'href': link,
                'body': f'SEC {actual_type} 申报索引；申报日期 {date_text}。{description}。'
                        '这是申报元数据，不含持仓明细。13F 是季度末快照，并非实时买卖记录；'
                        '没有持仓表和上期对照，不可据此认定新建仓、增减持、买入时间或持仓比例。',
                'source': 'SEC.gov', 'date': date_text, '_score': 1.0,
                '_filing_type': actual_type,
            })
        return results


class SearchAggregator:
    """Combine dated, scored search evidence with stable document deduplication."""

    def __init__(self, search_service: SearchService):
        self.search_service = search_service

    def aggregate(self, queries, max_results=None, timelimit='w', delay_range=None,
                  min_score=0.3, initial_results=None):
        return asyncio.run(self.aggregate_async(queries, max_results, timelimit, delay_range,
                                                min_score, initial_results))

    async def aggregate_async(self, queries, max_results=None, timelimit='w', delay_range=None,
                              min_score=0.3, initial_results=None, return_records=False):
        config = self.search_service.config
        max_results = config.MAX_RESULTS if max_results is None else max_results
        delay_range = config.SEARCH_DELAY_RANGE if delay_range is None else delay_range
        queries = list(dict.fromkeys(q.strip() for q in queries
                                    if isinstance(q, str) and q.strip()))[:config.MAX_QUERIES]
        semaphore = asyncio.Semaphore(min(10, max(1, config.SEARCH_CONCURRENCY)))

        async def search(query):
            async with semaphore:
                if delay_range and delay_range[1] > 0:
                    await asyncio.sleep(1)
                try:
                    return await asyncio.to_thread(self.search_service.search, query, max_results, timelimit)
                except Exception as exc:
                    print(f' ⚠️ 单个搜索失败: {type(exc).__name__}')
                    return None

        found = await asyncio.gather(*(search(query) for query in queries))
        results = list(initial_results or [])
        successes = bool(results) or any(items is not None for items in found)
        for items in found:
            results.extend(items or [])
        if return_records:
            if not successes:
                raise RuntimeError('所有搜索后端均不可用或失败，无法核实资本动态')
            return results, successes
        return self._format_results(results, successes, min_score)

    def _format_results(self, results, successes, min_score=0.3):
        config = self.search_service.config
        results = self.search_service._process_results(results)
        # Confirmed dates first, but retain unknown-date evidence for sparse analyses.
        results.sort(key=lambda item: published_date(item.get('date', '')) is None)
        parts = []
        size = 0
        for item in results:
            if item['_score'] < min_score:
                continue
            date = item['date'] or '未提供'
            if published_date(item['date']) is None:
                date += '（时效未核实）'
            part = (f"时间: {date}\n"
                    f"信源: {item['source']}\n标题: {item['title']}\n"
                    f"链接: {item['href']}\n摘要: {item['body'][:4000]}\n")
            if size + len(part) + 5 > config.MAX_RAW_DATA_CHARS:
                break
            parts.append(part)
            size += len(part) + 5
        if not parts and not successes:
            raise RuntimeError('所有搜索后端均不可用或失败，无法核实资本动态')
        return '\n---\n'.join(parts)


class InvestmentSearchAggregator(SearchAggregator):
    """Combine SEC evidence and financial web evidence using the same filters."""

    def __init__(self, search_service: SearchService, config: Config = None):
        super().__init__(search_service)
        self.config = config or search_service.config
        self.sec_searcher = SecEdgarSearcher(self.config)

    def aggregate(self, queries, funds=None, max_results=None, delay_range=None, timelimit=None):
        return asyncio.run(self.aggregate_async(queries, funds, max_results, delay_range, timelimit))

    async def aggregate_async(self, queries, funds=None, max_results=None, delay_range=None, timelimit=None):
        async def filings(fund):
            try:
                return await asyncio.to_thread(
                    self.sec_searcher.search_recent_filings,
                    company_name=fund, filing_types=['13F-HR', '13F-NT', 'SC 13G', 'SC 13D'], max_results=3)
            except Exception as exc:
                print(f' ⚠️ SEC 检索失败，继续通用搜索: {type(exc).__name__}')
                return []

        # Reuse the common merge/format path while SEC and web searches overlap.
        web_task = super().aggregate_async(
            queries, max_results, self.config.NEWS_TIMELIMIT if timelimit is None else timelimit,
            delay_range, return_records=True)
        found = await asyncio.gather(web_task, *(filings(fund) for fund in dict.fromkeys(funds or [])),
                                     return_exceptions=True)
        web, sec = found[0], [item for group in found[1:] if isinstance(group, list) for item in group]
        if isinstance(web, Exception):
            if not sec:
                raise web
            return self._format_results(sec, True)
        # Keep structured records for one unified deduplication/context cap.
        # The common async helper can return its records internally without another search.
        records, successes = web
        return self._format_results(records + sec, successes or bool(sec))
