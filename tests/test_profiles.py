import json
from unittest.mock import Mock

import pytest

import run
from config.research_profiles import load_profile


def write_profile(tmp_path, **changes):
    data = {'schema_version': 1, 'tracker': 'all',
            'topics': {'investment': [' Himalaya Capital ', 'Himalaya Capital'],
                       'investment_china': ['高毅资产']},
            'options': {'max_queries': 3, 'max_results': 2, 'max_workers': 2,
                        'no_cache': True, 'sec_holdings': True}}
    data.update(changes)
    path = tmp_path / '研究.json'
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def test_profile_market_lists_and_explicit_cli_precedence(tmp_path, monkeypatch):
    path = write_profile(tmp_path)
    pipeline = Mock()
    monkeypatch.setattr(run, 'run_tracker', pipeline)
    monkeypatch.setattr('sys.argv', ['run.py', '--profile', str(path), '--max-queries', '1'])
    run.main()
    assert [c.kwargs['topics'] for c in pipeline.call_args_list] == [['Himalaya Capital'], ['高毅资产']]
    for call in pipeline.call_args_list:
        cfg = call.kwargs['config']
        assert cfg.MAX_QUERIES == 1 and cfg.MAX_RESULTS == 2
        assert cfg.SEC_INCLUDE_HOLDINGS and not cfg.ENABLE_SEARCH_CACHE
        assert call.kwargs['max_workers'] == 2


def test_dry_run_does_not_construct_services_or_write(tmp_path, monkeypatch, capsys):
    path = write_profile(tmp_path)
    before = list(tmp_path.iterdir())
    forbidden = Mock(side_effect=AssertionError('dry-run invoked service'))
    monkeypatch.setattr(run, 'run_tracker', forbidden)
    monkeypatch.setattr(run, 'SearchService', forbidden)
    monkeypatch.setattr('sys.argv', ['run.py', '--profile', str(path), '--tracker', 'investment_china',
                                   '--topics', '易方达', '--dry-run'])
    run.main()
    plan = json.loads(capsys.readouterr().out)
    assert plan['query_budget'] == 3
    assert plan['markets'][0]['topics'] == ['易方达']
    assert len(plan['markets']) == 1 and plan['network_checked'] is False
    forbidden.assert_not_called()
    assert list(tmp_path.iterdir()) == before


@pytest.mark.parametrize('change', [
    {'schema_version': True}, {'schema_version': 2}, {'email': 'someone@example.com'},
    {'tracker': 'science'}, {'name': 42}, {'topics': {'investment': []}},
    {'topics': {'investment': [' ']}}, {'topics': {'investment': [None]}},
    {'topics': {'other': ['Fund']}}, {'topics': ['Fund']},
    {'options': {'max_queries': True}}, {'options': {'max_results': 21}},
    {'options': {'max_workers': 0}}, {'options': {'no_cache': 'false'}},
    {'options': {'timelimit': []}}, {'options': {'search_backends': ['bogus']}},
    {'options': {'search_backends': []}}, {'options': {'BRAVE_API_KEY': 'secret'}},
])
def test_invalid_profile_rejected_before_any_search(tmp_path, monkeypatch, change):
    path = write_profile(tmp_path, **change)
    forbidden = Mock()
    monkeypatch.setattr(run, 'run_tracker', forbidden)
    monkeypatch.setattr('sys.argv', ['run.py', '--profile', str(path)])
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert exc.value.code == 2
    forbidden.assert_not_called()


def test_profile_bounds_and_duplicate_backends(tmp_path):
    path = write_profile(tmp_path, options={'search_backends': ['gdelt', 'gdelt', 'ddgs_text']})
    assert load_profile(path)['options']['search_backends'] == ['gdelt', 'ddgs_text']
    path.write_text(' ' * (128 * 1024 + 1))
    with pytest.raises(ValueError, match='128 KiB'):
        load_profile(path)


def test_profile_missing_and_malformed_files(tmp_path, monkeypatch):
    path = tmp_path / 'missing.json'
    monkeypatch.setattr('sys.argv', ['run.py', '--profile', str(path), '--dry-run'])
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert exc.value.code == 2
    path.write_text('{')
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert exc.value.code == 2


@pytest.mark.parametrize('domain', ['https://example.com', '*.example.com', 'example.com:443',
                                  'example.com/path', 'localhost', '-bad.example', 'bad_.example', '', 'a..example'])
def test_domain_filters_reject_urls_and_ambiguous_rules(monkeypatch, domain):
    forbidden = Mock()
    monkeypatch.setattr(run, 'run_tracker', forbidden)
    monkeypatch.setattr('sys.argv', ['run.py', '--allow-domains', domain, '--dry-run'])
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert exc.value.code == 2
    forbidden.assert_not_called()


def test_profile_source_filters_and_evidence_only_reach_config(tmp_path, monkeypatch):
    path = write_profile(tmp_path, options={'allow_domains': ['SEC.GOV.', 'sec.gov'],
                                          'deny_domains': ['unwanted.example'], 'evidence_only': True})
    pipeline = Mock()
    monkeypatch.setattr(run, 'run_tracker', pipeline)
    monkeypatch.setattr('sys.argv', ['run.py', '--profile', str(path), '--tracker', 'investment',
                                   '--allow-domains', 'cninfo.com.cn'])
    run.main()
    config = pipeline.call_args.kwargs['config']
    assert config.SOURCE_ALLOWLIST == {'cninfo.com.cn'}
    assert 'unwanted.example' in config.SOURCE_DENYLIST and 'ads.' in config.SOURCE_DENYLIST
    assert config.EVIDENCE_ONLY


def test_evidence_only_never_constructs_model_client(monkeypatch):
    from trackers.investment import InvestmentTracker
    from config.settings import Config, LLMConfig
    config = Config()
    config.DEEPSEEK = LLMConfig('configured-secret', 'https://example.com', 'test')
    config.EVIDENCE_ONLY = True
    forbidden = Mock(side_effect=AssertionError('model client constructed'))
    monkeypatch.setattr(LLMConfig, 'create_client', forbidden)
    tracker = InvestmentTracker(config)
    assert tracker.llm.client is None
    assert tracker.generate_queries('Fund')
    tracker.llm.complete = Mock(side_effect=AssertionError('model must remain unused'))
    assert tracker.generate_queries('Fund')
    assert tracker.summarize('Fund', '链接: https://example.com/a\n摘要: evidence').startswith('### 仅检索证据')
    assert config.DEEPSEEK.api_key == 'configured-secret'
    forbidden.assert_not_called()


def test_boolean_cli_overrides_can_disable_profile_options(tmp_path, monkeypatch):
    path = write_profile(tmp_path, options={'sec_holdings': True, 'evidence_only': True,
                                          'no_cache': True, 'no_checkpoints': True})
    pipeline = Mock()
    monkeypatch.setattr(run, 'run_tracker', pipeline)
    monkeypatch.setattr('sys.argv', ['run.py', '--profile', str(path), '--tracker', 'investment',
                                   '--no-sec-holdings', '--no-evidence-only', '--cache', '--checkpoints'])
    run.main()
    config = pipeline.call_args.kwargs['config']
    assert not config.SEC_INCLUDE_HOLDINGS and not config.EVIDENCE_ONLY
    assert config.ENABLE_SEARCH_CACHE and config.ENABLE_TOPIC_CHECKPOINTS


def test_brief_profile_and_cli_full_override(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'brief.json'
    path.write_text(json.dumps({'schema_version': 1, 'tracker': 'investment',
                               'topics': {'investment': ['Himalaya Capital']},
                               'options': {'report_style': 'brief'}}))
    monkeypatch.setattr('sys.argv', ['run.py', '--profile', str(path), '--dry-run'])
    run.main()
    assert json.loads(capsys.readouterr().out)['report_style'] == 'brief'
    monkeypatch.setattr('sys.argv', ['run.py', '--profile', str(path), '--report-style', 'full', '--dry-run'])
    run.main()
    assert json.loads(capsys.readouterr().out)['report_style'] == 'full'


def test_invalid_report_style_is_rejected(tmp_path):
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps({'schema_version': 1, 'options': {'report_style': 'unknown'}}))
    with pytest.raises(ValueError, match='report_style'):
        load_profile(path)
