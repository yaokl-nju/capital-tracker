from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

from config.settings import Config
from core.search_service import (
    InvestmentSearchAggregator, SearchAggregator, SearchService, SecEdgarSearcher,
    SimpleCache, calculate_source_score, canonical_url,
)


@pytest.fixture
def config():
    config = Config()
    config.SEARCH_DELAY_RANGE = (0, 0)
    config.SEARCH_PROXY = ''
    config.SEARCH_MIN_INTERVALS = {}
    config.SEC_USER_AGENT = 'FinanceTests/1.0 tests@example.com'
    return config


def item(url='https://reuters.com/a', title='Fund investment update', **kwargs):
    return dict(href=url, title=title, body='Invested USD 10 million', source='Reuters', **kwargs)


def test_cache_ttl_limits_isolation_and_update(monkeypatch):
    now = [1.0]
    monkeypatch.setattr('core.search_service.time.monotonic', lambda: now[0])
    cache = SimpleCache(2, 10)
    cache.set('q', 'w', [item()], 1)
    assert cache.get('q', 'w', 5) is None
    result = cache.get('q', 'w', 1)
    result[0]['title'] = 'mutated'
    assert cache.get('q', 'w', 1)[0]['title'] != 'mutated'
    cache.set('other', 'w', [])
    cache.set('q', 'w', [item()], 1)
    assert cache.get('other', 'w') == []
    now[0] = 12
    assert cache.get('q', 'w', 1) is None
    cache.clear()
    assert not cache.cache


def test_cache_concurrent_access():
    cache = SimpleCache(max_size=7)
    def access(i):
        cache.set(str(i % 11), 'w', [item()])
        return cache.get(str(i % 11), 'w')
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(access, range(500)))
    assert len(cache.cache) <= 7


@pytest.mark.parametrize(('domain', 'allow', 'deny', 'score'), [
    ('reuters.com', {'sec.gov'}, set(), 0),
    ('reuters.com.fake.org', set(), set(), 0.5),
    ('notsec.gov', set(), set(), 0.5),
    ('www.sec.gov', {'sec.gov'}, set(), 1),
    ('www.reuters.com', set(), {'reuters.com'}, 0),
    ('ads.example.com', set(), {'ads.'}, 0),
    ('sse.com.cn', set(), set(), 1),
])
def test_source_domain_boundaries(domain, allow, deny, score):
    assert calculate_source_score('SEC.gov', domain, allow, deny) == score


@pytest.mark.parametrize(('url', 'expected'), [
    ('https://EXAMPLE.com/a?utm_source=x&id=3#part', 'https://example.com/a?id=3'),
    ('https://example.com/a?id=4', 'https://example.com/a?id=4'),
    ('file:///private/a', ''), ('javascript:alert(1)', ''),
    ('https://user:pass@example.com/a', ''), ('https://[bad/', ''),
])
def test_url_normalization(url, expected):
    assert canonical_url(url) == expected


def test_result_cleaning_scores_and_event_dedup(config):
    service = SearchService(config)
    results = service._process_results([
        item(title='Fund Q1 holding 5%'), item(url='https://reuters.com/a?utm_source=x', title='duplicate'),
        item(url='https://sec.gov/b', title='Fund Q2 holding 6%'),
        item(url='https://example.com/c', title='Fund Q1 holding 5%'),
        item(url='https://example.com/d', title='Fund Q1 holding 7%'),
        item(url='https://example.com/e', title='Fund Q1 increased holding 5%'),
        {'title': None, 'href': 'https://a.com'}, None,
        item(url=''), item(url='ftp://example.com/a'),
    ])
    assert len(results) == 4
    assert results[0]['_score'] == 1
    assert all(r['_score'] > 0 and r['date'] == '' for r in results)


def test_backend_fallback_and_result_limit_cache(config):
    config.SEARCH_BACKENDS = ['unknown', 'serper', 'ddgs', 'brave']
    config.SERPER_API_KEY = ''
    config.BRAVE_API_KEY = 'test'
    service = SearchService(config)
    service._try_ddgs = Mock(side_effect=RuntimeError('temporary failure'))
    service._try_brave = Mock(side_effect=lambda q, n, t: [item(url=f'https://example.com/{i}', title=f'Event {i}') for i in range(n)])
    assert service._enabled_backends() == ['ddgs', 'brave']
    assert len(service.search('fund', 1)) == 1
    assert len(service.search('fund', 3)) == 3
    assert len(service.search('fund', 1)) == 1
    assert service._try_brave.call_count == 2
    assert service.search('  ', 3) == []


def test_aggregator_filters_and_limits_queries(config):
    config.MAX_QUERIES = 2
    config.SOURCE_DENYLIST = {'blocked.com'}
    service = SearchService(config)
    service.search = Mock(return_value=[item(), item(url='https://blocked.com/a', title='Bad')])
    text = SearchAggregator(service).aggregate(['q', 'q', '', 'next', 'extra'])
    assert service.search.call_count == 2
    assert text.count('链接:') == 1
    assert 'blocked.com' not in text
    assert '时效未核实' in text


def test_aggregator_reports_backend_failure(config):
    service = SearchService(config)
    service.search = Mock(return_value=None)
    with pytest.raises(RuntimeError, match='搜索后端'):
        SearchAggregator(service).aggregate(['q'])
    service.search.return_value = []
    assert SearchAggregator(service).aggregate(['q']) == ''


def sec_row(form, date, path):
    return f'<tr><td>{form}</td><td><a href="{path}">Documents</a></td><td>Report period</td><td>{date}</td></tr>'


def test_sec_row_boundaries_actual_types_and_dates(config):
    searcher = SecEdgarSearcher(config)
    today = date.today().isoformat()
    previous = (date.today() - timedelta(days=3)).isoformat()
    old = (date.today() - timedelta(days=999)).isoformat()
    future = (date.today() + timedelta(days=1)).isoformat()
    html = '<table>' + ''.join([
        sec_row('13F-HR', today, '/Archives/edgar/data/1/a-index.html'),
        sec_row('13F-HR/A', previous, '/Archives/edgar/data/1/b-index.html'),
        sec_row('13F-NT', today, '/Archives/edgar/data/1/c-index.html'),
        sec_row('13F-HR', old, '/Archives/edgar/data/1/old-index.html'),
        sec_row('13F-HR', future, '/Archives/edgar/data/1/future-index.html'),
        '<tr><td>13F-HR</td><td>2026-01-01</td></tr>',
        '<tr><td>13F-HR</td><td><a href="/Archives/edgar/data/1/nodate">Doc</a></td></tr>',
        sec_row('13F-HR', 'invalid', '/Archives/edgar/data/1/invalid'),
    ]) + '</table>'
    found = searcher._parse_general_filings(html, '13F-HR')
    assert [f['date'] for f in found] == [today, previous]
    assert found[0]['href'] == 'https://www.sec.gov/Archives/edgar/data/1/a-index.html'
    assert '不含持仓明细' in found[0]['body']


def test_sec_sort_dedup_and_mapping(monkeypatch, config):
    today = date.today().isoformat()
    older = (date.today() - timedelta(days=3)).isoformat()
    monkeypatch.setattr('core.search_service.get_sec_cik', lambda name: None)
    def request(*args, **kwargs):
        assert kwargs['params']['company'] == 'Himalaya Capital Management LLC'
        form = kwargs['params']['type']
        response = Mock()
        response.text = sec_row(form, older if form == '13F-HR' else today,
                                '/Archives/edgar/data/1/' + form.replace(' ', '') + '-index.html')
        return response
    monkeypatch.setattr('core.search_service.requests.get', request)
    monkeypatch.setattr('core.search_service.time.sleep', lambda _: None)
    found = SecEdgarSearcher(config).search_recent_filings('Himalaya Capital', max_results=2)
    assert len(found) == 2
    assert all(f['date'] == today for f in found)
    assert all(f['title'].startswith('Himalaya Capital') for f in found)


def test_sec_failure_does_not_block_web(config):
    service = SearchService(config)
    service.search = Mock(return_value=[item()])
    aggregator = InvestmentSearchAggregator(service, config)
    aggregator.sec_searcher.search_recent_filings = Mock(side_effect=RuntimeError('offline'))
    assert 'Reuters' in aggregator.aggregate(['q'], funds=['Fund'])


def test_all_sources_obey_allowlist(config):
    config.SOURCE_ALLOWLIST = {'reuters.com'}
    service = SearchService(config)
    service.search = Mock(return_value=[item()])
    aggregator = InvestmentSearchAggregator(service)
    aggregator.sec_searcher.search_recent_filings = Mock(return_value=[item(url='https://sec.gov/a')])
    assert 'sec.gov' not in aggregator.aggregate(['q'], funds=['Fund'])


@pytest.mark.parametrize('timelimit,expected', [('d', 'pd'), ('w', 'pw'), ('m', 'pm'), ('y', 'py'), (None, None)])
def test_brave_freshness_country_and_optional_fields(monkeypatch, config, timelimit, expected):
    config.BRAVE_API_KEY = 'fake'
    config.SEARCH_GL = 'CN'
    config.SEARCH_HL = 'zh-Hans'
    response = Mock()
    response.json.return_value = {'web': {'results': [{'title': '资本事件', 'url': 'https://sse.com.cn/a'}]}}
    get = Mock(return_value=response)
    monkeypatch.setattr('core.search_service.requests.get', get)
    found = SearchService(config)._try_brave('query', 100, timelimit)
    params = get.call_args.kwargs['params']
    assert params.get('freshness') == expected
    assert params['count'] == 20 and params['country'] == 'CN' and params['search_lang'] == 'zh-hans'
    assert found[0]['body'] == ''


def test_serper_timelimit_and_proxy(monkeypatch, config):
    config.SERPER_API_KEY = 'fake'
    config.SEARCH_PROXY = 'http://localhost:8000'
    response = Mock()
    response.json.return_value = {'organic': [{'title': '融资', 'link': 'https://example.com/a', 'snippet': 'Series A', 'date': '1 day ago'}]}
    post = Mock(return_value=response)
    monkeypatch.setattr('core.search_service.requests.post', post)
    found = SearchService(config)._try_serper('query', 5, 'd')
    assert post.call_args.kwargs['json']['tbs'] == 'qdr:d'
    assert post.call_args.kwargs['proxies']['https'] == config.SEARCH_PROXY
    assert found[0]['date'] == '1 day ago'


@pytest.mark.parametrize('time_range', ['d', 'w', 'm', 'y', None])
def test_tavily_time_ranges(monkeypatch, config, time_range):
    config.TAVILY_API_KEY = 'fake'
    response = Mock()
    response.json.return_value = {'results': [{'title': 'Funding', 'url': 'https://example.com/a', 'content': 'fundraising', 'published_date': '2026-09-28'}]}
    post = Mock(return_value=response)
    monkeypatch.setattr('core.search_service.requests.post', post)
    found = SearchService(config)._try_tavily('q', 30, time_range)
    payload = post.call_args.kwargs['json']
    assert payload['max_results'] == 20
    assert 'api_key' not in payload
    assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer fake'
    assert payload['topic'] == 'news' and payload['include_published_date'] is True
    assert payload.get('time_range') == {'d': 'day', 'w': 'week', 'm': 'month', 'y': 'year', None: None}[time_range]
    assert found[0]['date'] == '2026-09-28'


def test_ddgs_uses_single_explicit_engine(monkeypatch, config):
    ddgs = Mock()
    ddgs.news.side_effect = RuntimeError('news error')
    ddgs.text.return_value = [{'title': 'Capital investment', 'href': 'https://reuters.com/a', 'body': 'funding'}]
    context = Mock()
    context.__enter__ = Mock(return_value=ddgs)
    context.__exit__ = Mock(return_value=False)
    constructor = Mock(return_value=context)
    monkeypatch.setattr('core.search_service.DDGS', constructor)
    found = SearchService(config)._try_ddgs('q', 5, 'w')
    assert found[0]['href'] == 'https://reuters.com/a'
    assert ddgs.news.call_count == 0 and ddgs.text.call_count == 1
    assert ddgs.text.call_args.kwargs['backend'] == 'duckduckgo'
    assert constructor.call_args.kwargs['timeout'] == config.SEARCH_TIMEOUT


def test_ddgs_single_call_still_obeys_source_filter(monkeypatch, config):
    config.SOURCE_ALLOWLIST = {'sec.gov'}
    ddgs = Mock()
    ddgs.news.return_value = [{'title': 'Other', 'url': 'https://example.com/a'}]
    ddgs.text.return_value = [{'title': 'Filing', 'href': 'https://sec.gov/a'}]
    context = Mock()
    context.__enter__ = Mock(return_value=ddgs)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr('core.search_service.DDGS', Mock(return_value=context))
    assert SearchService(config)._try_ddgs('q', 5, 'w')[0]['source'] == 'sec.gov'
    assert ddgs.text.call_count == 1


def test_search_distinguishes_empty_success_and_failure(config):
    config.SEARCH_BACKENDS = ['ddgs']
    service = SearchService(config)
    service._try_ddgs = Mock(return_value=[])
    assert service.search('q') == []
    service._try_ddgs.return_value = None
    assert service.search('q') is None


def test_context_limit_preserves_complete_records(config):
    config.MAX_RAW_DATA_CHARS = 150
    service = SearchService(config)
    service.search = Mock(return_value=[item(title='Event 1'), item(url='https://reuters.com/b', title='Event 2')])
    text = SearchAggregator(service).aggregate(['q'])
    assert len(text) <= config.MAX_RAW_DATA_CHARS
    assert text.count('摘要:') == 1


def test_known_stale_results_do_not_block_fallback(config):
    config.SEARCH_BACKENDS = ['ddgs', 'brave']
    config.BRAVE_API_KEY = 'fake'
    service = SearchService(config)
    old = (date.today() - timedelta(days=60)).isoformat()
    service._try_ddgs = Mock(return_value=[item(date=old)])
    service._try_brave = Mock(return_value=[item(url='https://sec.gov/b', title='Current', date=date.today().isoformat())])
    found = service.search('q', timelimit='w')
    assert found[0]['title'] == 'Current'
    assert service._try_brave.call_count == 1


def test_unknown_dates_are_kept_but_labeled(config):
    service = SearchService(config)
    service.search = Mock(return_value=[item(date='unknown format')])
    assert 'unknown format（时效未核实）' in SearchAggregator(service).aggregate(['q'])


@pytest.mark.parametrize(('first', 'second'), [
    ('Fund raises $1 million', 'Fund raises $1 billion'),
    ('Fund completes Series A financing', 'Fund completes Series B financing'),
    ('企业完成人民币1亿元融资', '企业完成人民币1万元融资'),
])
def test_financial_events_not_merged_by_similar_titles(config, first, second):
    service = SearchService(config)
    assert len(service._process_results([item(title=first), item(url='https://reuters.com/b', title=second)])) == 2


def test_same_headline_different_dates_or_body_amounts_are_preserved(config):
    service = SearchService(config)
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    assert len(service._process_results([
        item(date=today), item(url='https://reuters.com/b', date=yesterday),
        dict(item(url='https://reuters.com/c', date=today), body='USD 20 million'),
    ])) == 3


@pytest.mark.parametrize('url', ['https://example.com:bad/a', 'https://example.com:70000/a', 'https://example.com/a\nsecret'])
def test_malformed_ports_and_controls_are_rejected(url):
    assert canonical_url(url) == ''


def test_serper_ignores_malformed_individual_entries(monkeypatch, config):
    config.SERPER_API_KEY = 'fake'
    response = Mock()
    response.json.return_value = {'organic': [None, {'link': 'https://example.com/missing-title'},
                                             {'title': 'Capital', 'link': 'https://reuters.com/a'}]}
    monkeypatch.setattr('core.search_service.requests.post', Mock(return_value=response))
    assert len(SearchService(config)._try_serper('q', 5, 'w')) == 1


def test_verified_cik_submissions_dates_forms_and_identity(monkeypatch, config):
    today = date.today().isoformat()
    old = (date.today() - timedelta(days=200)).isoformat()
    response = Mock()
    response.json.return_value = {
        'cik': '1709323', 'name': 'Himalaya Capital Management LLC',
        'filings': {'recent': {
            'form': ['13F-HR', 'SCHEDULE 13G/A', '13F-NT', '13F-HR', '10-K'],
            'filingDate': [today, today, today, old, today],
            'accessionNumber': [f'0002043585-26-00000{i}' for i in range(5)],
            'primaryDocument': ['primary_doc.xml'] * 5,
            'reportDate': ['2026-06-30'] * 5,
        }},
    }
    get = Mock(return_value=response)
    monkeypatch.setattr('core.search_service.requests.get', get)
    results = SecEdgarSearcher(config).search_recent_filings('Himalaya Capital', max_results=10)
    assert len(results) == 3
    assert get.call_count == 1
    assert get.call_args.args[0] == 'https://data.sec.gov/submissions/CIK0001709323.json'
    assert all('/1709323/' in result['href'] and '2026-06-30' in result['body'] for result in results)
    assert any('通知文件' in result['body'] for result in results)


def test_submissions_misaligned_columns_stop_without_retry(monkeypatch, config):
    response = Mock()
    response.json.return_value = {'filings': {'recent': {
        'form': ['13F-HR'], 'filingDate': [], 'accessionNumber': ['0002043585-26-000001'],
        'primaryDocument': ['primary.xml']}}}
    monkeypatch.setattr('core.search_service.requests.get', Mock(return_value=response))
    assert SecEdgarSearcher(config)._search_submissions('0001709323', ['13F-HR'], 3, 120) == []


def test_submissions_unsafe_or_incomplete_records_are_skipped(monkeypatch, config):
    response = Mock()
    today = date.today().isoformat()
    response.json.return_value = {'filings': {'recent': {
        'form': ['13F-HR'] * 3, 'filingDate': [today] * 3,
        'accessionNumber': ['bad-accession', '0002043585-26-000001', '0002043585-26-000002'],
        'primaryDocument': ['a.xml', '../private.xml', 'primary.xml']}}}
    monkeypatch.setattr('core.search_service.requests.get', Mock(return_value=response))
    result = SecEdgarSearcher(config)._search_submissions('0001709323', ['13F-HR'], 3, 120)
    assert len(result) == 1 and result[0]['href'].endswith('/primary.xml')


def test_explicit_cik_and_verified_aliases():
    from config.fund_mappings import get_sec_cik
    assert get_sec_cik('himalaya capital') == '0001709323'
    assert get_sec_cik('1709323') == '0001709323'
    assert get_sec_cik('0') is None
    assert get_sec_cik('Unverified Fund') is None


@pytest.mark.parametrize('text,expected', [
    ('Sep 24, 2026', date(2026, 9, 24)),
    ('September 24, 2026', date(2026, 9, 24)),
    ('2026年09月24日', date(2026, 9, 24)),
    ('2026/09/24', date(2026, 9, 24)),
    ('昨天', date.today() - timedelta(days=1)),
    ('2天前', date.today() - timedelta(days=2)),
    ('1 week ago', date.today() - timedelta(days=7)),
    ('999999999999999999 years ago', None),
    ('bad date', None),
])
def test_publication_date_formats_and_untrusted_dates(text, expected):
    from core.search_service import published_date
    assert published_date(text) == expected


@pytest.mark.parametrize('text', ['48 hours ago', '48小时前', '2880分钟前'])
def test_relative_hours_are_not_all_classified_as_today(text):
    from core.search_service import published_date
    assert published_date(text) == date.today() - timedelta(days=2)


def test_same_disclosure_preserves_complementary_query_excerpts(config):
    first = item()
    first['body'] = 'Capital deployed USD 10 million'
    second = item(title='Financing detail')
    second['body'] = 'Debt financing USD 20 million'
    merged = SearchService(config)._process_results([first, second])
    assert len(merged) == 1
    assert 'USD 10 million' in merged[0]['body'] and 'USD 20 million' in merged[0]['body']
    assert first['body'] == 'Capital deployed USD 10 million'


def test_concurrent_backend_requests_are_spaced(config):
    import time
    config.SEARCH_MIN_INTERVALS = {'brave': 0.02}
    service = SearchService(config)
    started = []
    def backend(*args):
        started.append(time.monotonic())
        return []
    service._try_brave = backend
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: service._run_backend('brave', 'q', 3, 'w'), range(4)))
    assert len(started) == 4
    assert all(b - a >= 0.018 for a, b in zip(sorted(started), sorted(started)[1:]))


def test_ddgs_confirmed_empty_is_not_reported_as_outage(monkeypatch, config):
    from ddgs.exceptions import DDGSException
    ddgs = Mock()
    ddgs.news.side_effect = ddgs.text.side_effect = DDGSException('No results found.')
    context = Mock()
    context.__enter__ = Mock(return_value=ddgs)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr('core.search_service.DDGS', Mock(return_value=context))
    assert SearchService(config)._try_ddgs('q', 5, 'w') == []


def test_ddgs_news_calls_news_only_and_preserves_date(monkeypatch, config):
    ddgs = Mock()
    ddgs.news.return_value = [{'title': 'Capital news', 'url': 'https://reuters.com/news',
                               'body': 'Investment', 'date': date.today().isoformat()}]
    context = Mock()
    context.__enter__ = Mock(return_value=ddgs)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr('core.search_service.DDGS', Mock(return_value=context))
    found = SearchService(config)._try_ddgs_news('q', 5, 'w')
    assert found[0]['date'] == date.today().isoformat()
    ddgs.news.assert_called_once_with('q', backend='bing', region='us-en',
                                     safesearch=config.SEARCH_SAFESEARCH, max_results=5, timelimit='w')
    ddgs.text.assert_not_called()


@pytest.mark.parametrize('news_results', [None, [], 'success'])
def test_news_first_text_last_provider_fallback(config, news_results):
    config.SEARCH_BACKENDS = Config.SEARCH_BACKENDS
    config.SERPER_API_KEY = config.BRAVE_API_KEY = config.TAVILY_API_KEY = config.PARALLEL_SEARCH_API_KEY = 'fake'
    service = SearchService(config)
    called = []
    def backend(name):
        def call(*args):
            called.append(name)
            if name == 'ddgs_news':
                return [item()] if news_results == 'success' else news_results
            if name == 'ddgs_text':
                return [item()]
            return None
        return call
    for name in config.SEARCH_BACKENDS:
        setattr(service, f'_try_{name}', backend(name))
    assert service.search('q')
    assert called == (['ddgs_news'] if news_results == 'success' else config.SEARCH_BACKENDS)


@pytest.mark.parametrize('backend', ['brave', 'serper', 'tavily', 'parallel'])
@pytest.mark.parametrize('failure', ['timeout', 'json', 'response_type'])
def test_http_backends_fail_once_with_safe_logs(monkeypatch, config, capsys, backend, failure):
    import requests
    for attr in ('BRAVE_API_KEY', 'SERPER_API_KEY', 'TAVILY_API_KEY', 'PARALLEL_SEARCH_API_KEY'):
        setattr(config, attr, 'secret-must-not-be-logged')
    response = Mock()
    request = Mock(return_value=response)
    if failure == 'timeout':
        request.side_effect = requests.Timeout('secret-must-not-be-logged')
    elif failure == 'json':
        response.json.side_effect = ValueError('secret-must-not-be-logged')
    else:
        response.json.return_value = []
    monkeypatch.setattr(f'core.search_service.requests.{"get" if backend == "brave" else "post"}', request)
    assert getattr(SearchService(config), f'_try_{backend}')('fund', 3, 'w') is None
    request.assert_called_once()
    assert 'secret-must-not-be-logged' not in capsys.readouterr().out
