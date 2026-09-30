import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config.settings import Config
from core.report_generator import ReportGenerator


def test_worker_receives_no_research_credentials(monkeypatch, tmp_path):
    for name in ('DEEPSEEK_API_KEY', 'BRAVE_API_KEY', 'EMAIL_PASSWORD', 'SEC_USER_AGENT', 'SEARCH_PROXY'):
        monkeypatch.setenv(name, 'private-test-value')
    monkeypatch.setenv('LANG', 'en_US.UTF-8')
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    def run_worker(command, **kwargs):
        assert command[1].endswith('pdf_worker.py')
        assert kwargs['timeout'] == 180
        assert kwargs.get('shell', False) is False
        assert 'private-test-value' not in json.dumps(kwargs)
        assert kwargs['env']['XDG_CACHE_HOME'] == str(tmp_path / 'cache')
        assert kwargs['encoding'] == 'utf-8'
        Path(command[2]).write_bytes(b'%PDF-test')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr('core.report_generator.subprocess.run', run_worker)
    target = tmp_path / '研究.pdf'
    ReportGenerator(str(target)).create_pdf({'Fund': 'Chinese 金融资本'})
    assert target.read_bytes() == b'%PDF-test'


@pytest.mark.parametrize('code', [-11, 1])
def test_native_crash_preserves_previous_pdf_and_removes_partial(monkeypatch, tmp_path, code):
    target = tmp_path / 'report.pdf'
    target.write_bytes(b'previous report')
    def crash(command, **kwargs):
        Path(command[2]).write_bytes(b'partial PDF')
        return SimpleNamespace(returncode=code, stdout='secret-output', stderr='private-data')
    monkeypatch.setattr('core.report_generator.subprocess.run', crash)
    with pytest.raises(RuntimeError, match='异常退出') as exc:
        ReportGenerator(str(target)).create_pdf({'Fund': 'analysis'})
    assert 'secret' not in str(exc.value) and 'private' not in str(exc.value)
    assert target.read_bytes() == b'previous report'
    assert list(tmp_path.iterdir()) == [target]


def test_timeout_propagates_without_auto_retry(monkeypatch, tmp_path):
    process = Mock(side_effect=subprocess.TimeoutExpired(['worker'], 180, output='secret'))
    monkeypatch.setattr('core.report_generator.subprocess.run', process)
    with pytest.raises(RuntimeError, match='超过 180 秒'):
        ReportGenerator(str(tmp_path / 'report.pdf')).create_pdf({'Fund': 'analysis'})
    assert process.call_count == 1 and not list(tmp_path.iterdir())


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan'), True, '180'])
def test_invalid_render_timeouts_do_not_start_worker(monkeypatch, tmp_path, timeout):
    config = Config()
    config.PDF_RENDER_TIMEOUT = timeout
    process = Mock()
    monkeypatch.setattr('core.report_generator.subprocess.run', process)
    with pytest.raises(RuntimeError, match='PDF 生成失败'):
        ReportGenerator(str(tmp_path / 'report.pdf'), config=config).create_pdf({'Fund': 'analysis'})
    process.assert_not_called()
    assert not list(tmp_path.iterdir())
