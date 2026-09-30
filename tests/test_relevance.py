from unittest.mock import Mock

import pytest

from config.settings import Config
from core.search_service import SearchService


def item(title, body='', href='https://example.com/news'):
    return {'title': title, 'body': body, 'href': href}


@pytest.mark.parametrize('names,record,expected', [
    (['BlackRock', '贝莱德'], item('罗永浩税务新闻'), False),
    (['BlackRock', '贝莱德'], item('Fund investment', 'BLACKROCK allocated USD 10 million'), True),
    (['BlackRock', '贝莱德'], item('贝莱德公布资金流入'), True),
    (['BlackRock'], item('NotBlackRock investors'), False),
    (['Coatue Management'], item('Coatue backs startup'), True),
    (['Coatue Management'], item('Capital management investment news'), False),
    (['E Fund', '易方达基金'], item('易方达公布年报'), True),
    (['E Fund'], item('The fund raised capital'), False),
    (['Perseverance Asset Management', '高毅资产'], item('高毅新建仓'), True),
    (['Fund'], item('Generic test institution'), True),
    (['HHLR Advisors'], item('Capital disclosure', href='https://example.com/hhlr-advisors-report'), True),
    (['Lone Pine Capital'], item('Pine timber investment update'), False),
    (['Lone Pine Capital'], item('Lone Pine buys shares'), True),
    (['Whale Rock Capital'], item('Whale activity in crypto markets'), False),
    (['Whale Rock Capital'], item('Whale Rock backs startup'), True),
    (['Viking Global Investors'], item('Viking Therapeutics financing'), False),
    (['Viking Global Investors'], item('Viking Global increased stake'), True),
    (['D1 Capital Partners'], item('D1 invests in startup'), True),
    (['Capital Group'], item('Generic capital investment'), False),
    (['Capital Group'], item('Capital Group investor letter'), True),
    (['Himalaya Capital'], item('Himalaya Nutravedics IPO investment news'), False),
    (['Himalaya Capital'], item('Himalaya mountains tourism financing'), False),
    (['Himalaya Capital'], item('Himalaya Capital Management 13F holdings'), True),
    (['Perseverance Asset Management', '高毅资产'], item('NASA Perseverance investment mission'), False),
    (['Perseverance Asset Management', '高毅资产'], item('Perseverance: the virtue of investing'), False),
    (['Perseverance Asset Management', '高毅资产'], item('Perseverance Asset Management adds BEKE'), True),
    (['Springs Capital', '淡水泉'], item('Silver Springs property funding'), False),
    (['Springs Capital', '淡水泉'], item('Springs Capital investment letter'), True),
    (['Springs Capital', '淡水泉'], item('淡水泉投资动态'), True),
])
def test_visible_evidence_recognizes_aliases_and_boundaries(names, record, expected):
    assert SearchService._mentions_topic(record, names) == expected


def test_irrelevant_news_falls_back_instead_of_stopping_search():
    cfg = Config()
    cfg.SEARCH_BACKENDS = ['ddgs_news', 'ddgs_text']
    cfg.SEARCH_MIN_INTERVALS = {}
    service = SearchService(cfg)
    service._run_backend = Mock(side_effect=[
        [item('罗永浩税务新闻')], [item('BlackRock investment update', 'USD 10 million')],
    ])
    results = service.search('BlackRock latest investments', required_names=['BlackRock', '贝莱德'])
    assert results[0]['title'] == 'BlackRock investment update'
    assert results[0]['_backend'] == 'ddgs_text'
    assert [call.args[0] for call in service._run_backend.call_args_list] == ['ddgs_news', 'ddgs_text']


def test_cache_separates_topic_context_and_unscoped_requests():
    cfg = Config()
    cfg.SEARCH_BACKENDS = ['ddgs_news']
    service = SearchService(cfg)
    service._run_backend = Mock(return_value=[item('BlackRock investment update')])
    assert service.search('capital')
    assert service.search('capital', required_names=['Temasek']) == []
    assert service.search('capital', required_names=['BlackRock'])
    assert service._run_backend.call_count == 3
    assert service.search('capital', required_names=['BlackRock'])
    assert service._run_backend.call_count == 3


def test_backend_provenance_survives_cache_and_evidence_formatting():
    from core.search_service import SearchAggregator
    cfg = Config()
    cfg.SEARCH_BACKENDS = ['parallel']
    cfg.PARALLEL_SEARCH_API_KEY = 'test-key'
    service = SearchService(cfg)
    service._run_backend = Mock(return_value=[item('BlackRock capital update')])
    service.search('capital', required_names=['BlackRock'])
    cached = service.search('capital', required_names=['BlackRock'])
    assert service._run_backend.call_count == 1 and cached[0]['_backend'] == 'parallel'
    assert '检索渠道: parallel' in SearchAggregator(service)._format_results(cached, True)


def test_all_irrelevant_results_are_successful_empty_not_outage():
    cfg = Config()
    cfg.SEARCH_BACKENDS = ['ddgs_news']
    service = SearchService(cfg)
    service._run_backend = Mock(return_value=[item('Unrelated capital news')])
    assert service.search('BlackRock investment', required_names=['BlackRock']) == []
def test_observed_himalaya_identity_conflict_is_not_the_us_manager():
    from core.search_policy import mentions_topic
    from config.fund_mappings import get_search_names, get_identity_context
    names = get_search_names('Himalaya Capital')
    item = {'title': 'Himalaya Capital', 'body': 'Founded Year: 2022 Bengaluru Website Url: http://himalayacapital.in',
            'href': 'https://platform.tracxn.com/a/d/company/profile'}
    assert not mentions_topic(item, names)
    item['body'] += ' Compared with Li Lu of Himalaya Capital, founded in 1997'
    assert mentions_topic(item, names)
    assert 'Li Lu' in get_identity_context('Himalaya Capital')
    assert not mentions_topic({'title': '喜马拉雅完成融资，播客平台发展', 'body': '', 'href': 'https://example.com'}, names)
    assert mentions_topic({'title': '李录投资理念', 'body': '', 'href': 'https://example.com'}, names)


def test_interview_preparation_filtered_without_excluding_investor_interviews():
    from core.search_policy import mentions_topic
    names = ['Two Sigma']
    def result(title):
        return {'title': title, 'body': 'quantitative investment firm Two Sigma', 'href': 'https://example.com/a'}
    assert not mentions_topic(result('Two Sigma Software Engineer Interview Questions (Updated 2026)'), names)
    assert mentions_topic(result('Interview: Two Sigma investor explains portfolio strategy'), names)
    assert mentions_topic(result('Two Sigma acquires interview preparation startup'), names)
    assert not mentions_topic(result('Two Sigma 实习招聘与面经'), names)
