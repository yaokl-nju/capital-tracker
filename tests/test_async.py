import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config.settings import Config
from core.orchestrator import Orchestrator
from core.search_service import SearchAggregator, InvestmentSearchAggregator, SearchService, SearchRequestGate
from core.tracker import Tracker
from trackers.investment import InvestmentQueryGenerator, InvestmentSummarizer


def evidence(index=0, date_text=''):
    return {'href': f'https://sec.gov/event/{index}', 'title': f'Asset {index} position',
            'body': f'Asset {index} holding {index + 1}%', 'date': date_text, 'source': 'SEC.gov'}


def config():
    value = Config()
    value.SEARCH_DELAY_RANGE = (0, 0)
    value.SEARCH_MIN_INTERVALS = {}
    return value


def test_original_prompt_dimensions_and_format_preserved():
    query = InvestmentQueryGenerator().get_prompt('Fund')
    summary = InvestmentSummarizer().get_prompt('Fund', 'Evidence')
    for dimension in ['持仓披露文件', '新建仓与增持', '资产类别/赛道关键词', '投资逻辑', '具体高增长信号']:
        assert dimension in query
    assert '每个维度 5-8 个' in query
    for heading in ['#### 1. 🎯 核心持仓（二级市场）', '#### 2. 📊 行业/赛道聚焦',
                    '#### 3. 💡 投资逻辑拆解', '#### 4. 🚀 新建仓/大幅增持标的', '#### 5. 🇨🇳 中国相关资产']:
        assert heading in summary
    assert '| 股票/资产 | 持仓变动 | 持仓比例 | 投资理由 | 增长催化剂 |' in summary
    assert '3-5只股票' in summary and 'Markdown，表格+列表' in summary
    assert '只有未核实时效资料时也进行分析' in summary
    assert '来源时效性未核实' in summary
    assert '四个板块' not in summary and '不执行后面' not in summary


def test_grouped_queries_truncated_across_dimensions():
    cfg = config()
    cfg.MAX_QUERIES = 5
    llm = Mock()
    llm.complete.return_value = json.dumps({str(i): [f'{i}-first', f'{i}-second'] for i in range(5)})
    tracker = Tracker(llm, Mock(), InvestmentQueryGenerator(), InvestmentSummarizer(), cfg)
    assert tracker.generate_queries('Fund') == [f'{i}-first' for i in range(5)]


def test_sparse_unknown_evidence_reaches_analysis():
    cfg = config()
    service = SearchService(cfg)
    service.search = Mock(return_value=[evidence(0), evidence(1, date.today().isoformat())])
    data = SearchAggregator(service).aggregate(['q'])
    assert data.index('event/1') < data.index('event/0')
    assert '时效未核实' in data
    llm = Mock()
    llm.complete.return_value = '核心持仓 Asset 0，来源时效性未核实 [来源](https://sec.gov/event/0)'
    tracker = Tracker(llm, SearchAggregator(service), InvestmentQueryGenerator(), InvestmentSummarizer(cfg), cfg)
    summary = tracker.summarize('Fund', data)
    assert summary == llm.complete.return_value
    assert 'Asset 0' in llm.complete.call_args.args[0]
    assert '不足5条' in llm.complete.call_args.args[0]


def test_unknown_only_data_is_analyzed_not_returned_raw():
    cfg = config()
    service = SearchService(cfg)
    service.search = Mock(return_value=[evidence()])
    llm = Mock()
    llm.complete.side_effect = ['{"queries": ["q"]}', 'Asset 0 分析，来源时效性未核实 [来源](https://sec.gov/event/0)']
    tracker = Tracker(llm, SearchAggregator(service), InvestmentQueryGenerator(), InvestmentSummarizer(cfg), cfg)
    topic, summary = asyncio.run(tracker.process_topic_async('Fund'))
    assert topic == 'Fund'
    assert '来源时效性未核实' in summary and '原始检索证据' not in summary
    assert llm.complete.call_count == 2


def test_async_queries_overlap_and_remain_in_input_order():
    cfg = config()
    service = SearchService(cfg)
    lock = Lock()
    active = peak = 0
    def search(q, *args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.08)
        with lock:
            active -= 1
        return [evidence(int(q))]
    service.search = search
    started = time.monotonic()
    data = SearchAggregator(service).aggregate([str(i) for i in range(6)])
    elapsed = time.monotonic() - started
    assert peak == 6 and elapsed < 0.4
    assert [data.index(f'event/{i}') for i in range(6)] == sorted(data.index(f'event/{i}') for i in range(6))


def test_async_search_concurrency_config_is_bounded():
    cfg = config()
    cfg.SEARCH_CONCURRENCY = 2
    service = SearchService(cfg)
    lock = Lock()
    active = peak = 0
    def search(*args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return []
    service.search = search
    assert SearchAggregator(service).aggregate([str(i) for i in range(6)]) == ''
    assert peak == 2


def test_sec_and_web_overlap_with_one_shared_merge():
    cfg = config()
    service = SearchService(cfg)
    barrier = Barrier(2)
    def web(*args, **kwargs):
        barrier.wait(timeout=2)
        return [evidence(0)]
    def sec(*args, **kwargs):
        barrier.wait(timeout=2)
        return [evidence(0), evidence(1)]
    service.search = web
    aggregator = InvestmentSearchAggregator(service)
    aggregator.sec_searcher.search_recent_filings = sec
    assert aggregator.aggregate(['q'], ['Fund']).count('链接:') == 2


def test_sec_valid_data_survives_web_failure():
    service = SearchService(config())
    service.search = Mock(return_value=None)
    aggregator = InvestmentSearchAggregator(service)
    aggregator.sec_searcher.search_recent_filings = Mock(return_value=[evidence()])
    assert 'event/0' in aggregator.aggregate(['q'], ['Fund'])


def test_finance_confirmed_empty_does_not_become_outage():
    service = SearchService(config())
    service.search = Mock(return_value=[])
    aggregator = InvestmentSearchAggregator(service)
    aggregator.sec_searcher.search_recent_filings = Mock(return_value=[])
    assert aggregator.aggregate(['q'], ['Fund']) == ''
    service.search.return_value = None
    with pytest.raises(RuntimeError, match='所有搜索后端'):
        aggregator.aggregate(['q'], ['Fund'])


def test_failed_query_does_not_retry_or_cancel_siblings():
    service = SearchService(config())
    def search(q, *args):
        if q == 'bad':
            raise RuntimeError('failure')
        return [evidence()]
    service.search = Mock(side_effect=search)
    assert 'event/0' in SearchAggregator(service).aggregate(['bad', 'good'])
    assert service.search.call_count == 2


def test_all_service_instances_share_rate_and_inflight_gate(monkeypatch):
    gate = SearchRequestGate(limit=3)
    monkeypatch.setattr('core.search_service._SEARCH_REQUEST_GATE', gate)
    cfg = config()
    cfg.SEARCH_BACKENDS = ['ddgs']
    cfg.ENABLE_SEARCH_CACHE = False
    services = [SearchService(cfg), SearchService(cfg)]
    started = []
    lock = Lock()
    active = peak = 0
    def backend(*args):
        nonlocal active, peak
        with lock:
            started.append(time.monotonic())
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return []
    for service in services:
        service._try_ddgs = backend
    with ThreadPoolExecutor(9) as pool:
        list(pool.map(lambda i: services[i % 2].search(str(i)), range(9)))
    assert peak <= 3 and len(started) == 9
    started.sort()
    assert all(sum(0 <= other - stamp < 0.999 for other in started) <= 3 for stamp in started)
    # Slots must be released on exceptions as well.
    with pytest.raises(RuntimeError):
        with gate.request():
            raise RuntimeError('failure')
    assert gate.slots.acquire(blocking=False)
    gate.slots.release()


def test_async_orchestrator_uses_hook_and_limits_topics():
    active = peak = 0
    async def process(topic):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0.01)
        active -= 1
        return topic, 'summary'
    tracker = SimpleNamespace(config=config(), process_topic_async=process,
                              process_topic=Mock(side_effect=AssertionError('sync path used')))
    result = asyncio.run(Orchestrator(tracker).run_topics_async(['B', 'A', 'C'], 2))
    assert list(result) == ['B', 'A', 'C'] and peak == 2


def test_defaults_single_attempt_delay_and_ddgs_priorities():
    cfg = Config()
    assert cfg.MAX_TRIALS == 1 and cfg.SEARCH_DELAY_RANGE == (1, 1)
    assert cfg.SEARCH_BACKENDS == ['ddgs_news', 'brave', 'parallel', 'serper', 'tavily', 'ddgs_text']
    assert cfg.SEARCH_CONCURRENCY <= 10
    from core.search_service import _SEARCH_REQUEST_GATE, _SEC_REQUEST_GATE
    assert _SEARCH_REQUEST_GATE.limit == 10
    assert _SEC_REQUEST_GATE.limit == 10
    assert _SEARCH_REQUEST_GATE is not _SEC_REQUEST_GATE


@pytest.mark.parametrize('limit', [0, -1, 1.5, '10'])
def test_invalid_request_limits_fail_before_blocking(limit):
    with pytest.raises(ValueError, match='positive integer'):
        SearchRequestGate(limit)


def test_sec_and_general_independent_ten_request_pools(monkeypatch):
    from core.search_service import SecEdgarSearcher
    general_gate, sec_gate = SearchRequestGate(), SearchRequestGate()
    monkeypatch.setattr('core.search_service._SEARCH_REQUEST_GATE', general_gate)
    monkeypatch.setattr('core.search_service._SEC_REQUEST_GATE', sec_gate)
    barrier = Barrier(20)
    lock = Lock()
    started = {'general': [], 'sec': []}
    active = {'general': 0, 'sec': 0}
    peak = {'general': 0, 'sec': 0}
    def request(pool):
        with lock:
            index = len(started[pool])
            started[pool].append(time.monotonic())
            active[pool] += 1
            peak[pool] = max(peak[pool], active[pool])
        try:
            if index < 10:
                # Both independent pools must admit all ten requests at once.
                barrier.wait(timeout=3)
            time.sleep(0.01)
        finally:
            with lock:
                active[pool] -= 1
        return Mock()
    cfg = config()
    services = [SearchService(cfg), SearchService(cfg)]
    sec_searchers = [SecEdgarSearcher(cfg), SecEdgarSearcher(cfg)]
    for service in services:
        service._try_ddgs = lambda *args: (request('general'), [])[1]
    monkeypatch.setattr('core.search_service.requests.get', lambda *a, **kw: request('sec'))
    def call(index):
        if index % 2:
            return sec_searchers[(index // 2) % 2]._get('https://data.sec.gov/test')
        return services[(index // 2) % 2]._run_backend('ddgs', 'q', 3, 'w')
    with ThreadPoolExecutor(20) as executor:
        list(executor.map(call, range(22)))
    assert peak == {'general': 10, 'sec': 10}
    for times in started.values():
        assert len(times) == 11
        times.sort()
        assert all(sum(0 <= other - stamp < 0.99 for other in times) <= 10 for stamp in times)
        assert times[10] - times[0] >= 0.99
    assert general_gate is not sec_gate


def test_sec_gate_slot_released_on_http_failure(monkeypatch):
    from core.search_service import SecEdgarSearcher
    import requests
    gate = SearchRequestGate(limit=1)
    monkeypatch.setattr('core.search_service._SEC_REQUEST_GATE', gate)
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError('test error')
    monkeypatch.setattr('core.search_service.requests.get', lambda *a, **kw: response)
    with pytest.raises(requests.HTTPError):
        SecEdgarSearcher(config())._get('https://data.sec.gov/test')
    assert gate.slots.acquire(blocking=False)
    gate.slots.release()
