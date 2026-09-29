import json
from unittest.mock import Mock

import pytest

import run
from config.settings import Config, LLMConfig


def test_doctor_reports_presence_only_and_does_not_run_pipeline(monkeypatch, capsys):
    cfg = Config()
    cfg.DEEPSEEK = LLMConfig('private-model-key', 'https://example.com', 'test-model')
    cfg.SEC_USER_AGENT = 'private-contact@example.com'
    cfg.SEARCH_BACKENDS = ['gdelt']
    monkeypatch.setattr(run, 'Config', lambda: cfg)
    pipeline = Mock(side_effect=AssertionError('doctor invoked pipeline'))
    monkeypatch.setattr(run, 'run_tracker', pipeline)
    monkeypatch.setattr('sys.argv', ['run.py', '--doctor'])
    run.main()
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data['network_checked'] is False
    assert data['model_key_present'] is True and data['sec_user_agent_present'] is True
    assert data['locally_available_backends'] == ['gdelt']
    assert 'private-model-key' not in output and 'private-contact' not in output
    pipeline.assert_not_called()


def test_cli_bounded_overrides_reach_tracker_without_mutating_defaults(monkeypatch):
    pipeline = Mock()
    monkeypatch.setattr(run, 'run_tracker', pipeline)
    monkeypatch.setattr('sys.argv', ['run.py', '--tracker', 'investment_china', '--topics', '高毅资产',
                                   '--max-queries', '2', '--max-results', '3', '--timelimit', 'none',
                                   '--search-backends', 'gdelt', 'ddgs_text', 'gdelt', '--no-cache'])
    run.main()
    cfg = pipeline.call_args.kwargs['config']
    assert cfg.MAX_QUERIES == 2 and cfg.MAX_RESULTS == 3 and cfg.NEWS_TIMELIMIT is None
    assert cfg.SEARCH_BACKENDS == ['gdelt', 'ddgs_text'] and cfg.ENABLE_SEARCH_CACHE is False
    assert Config.MAX_QUERIES == 12 and 'gdelt' not in Config.SEARCH_BACKENDS


@pytest.mark.parametrize('argument,value', [('--max-queries', '0'), ('--max-queries', '51'),
                                           ('--max-results', '0'), ('--max-results', '21'),
                                           ('--search-backends', 'unknown')])
def test_cli_rejects_invalid_bounds_before_search(monkeypatch, argument, value):
    pipeline = Mock()
    monkeypatch.setattr(run, 'run_tracker', pipeline)
    monkeypatch.setattr('sys.argv', ['run.py', argument, value])
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert exc.value.code == 2
    pipeline.assert_not_called()
