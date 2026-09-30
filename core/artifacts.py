"""Small, atomic report sidecars for inspecting the evidence behind a PDF."""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from .evidence_quality import coverage_summary, evidence_index, topic_notice
from .citations import validate_citations


def atomic_write(path, content):
    """Replace a UTF-8 file only after the complete content is written."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=path.parent, prefix='.report-',
                                         suffix='.tmp', delete=False) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def report_markdown(title, summaries, records, overview=''):
    sections = []
    for topic, summary in summaries.items():
        notice = ''
        if topic in records:
            urls = [item['url'] for item in evidence_index(records[topic].get('raw_evidence', ''))]
            summary, _ = validate_citations(summary, urls)
            notice = topic_notice(records[topic]) + '\n\n'
        sections.append(f'## {topic}\n\n{notice}{summary}')
    return f'# {title}\n\n' + (f'## 资料覆盖概览\n\n{overview}\n\n' if overview else '') + '\n\n'.join(sections) + '\n'


def save_report_artifacts(pdf_path, title, summaries, records, config, render_status='pending'):
    """Persist analysis and input evidence, never serializing configuration secrets."""
    base = Path(pdf_path).with_suffix('')
    overview = coverage_summary(summaries, records)
    markdown = report_markdown(title, summaries, records, overview)
    payload = {
        'schema_version': 1,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'title': title,
        'report_status': {'pdf': render_status},
        'settings': {
            'model': config.DEEPSEEK.model,
            'search_backends': list(config.SEARCH_BACKENDS),
            'news_timelimit': config.NEWS_TIMELIMIT,
            'analysis_mode': 'evidence_only' if config.EVIDENCE_ONLY else 'model_if_available',
            'report_style': config.REPORT_STYLE,
            'source_allowlist': sorted(config.SOURCE_ALLOWLIST),
            'source_denylist': sorted(config.SOURCE_DENYLIST),
            'max_queries': config.MAX_QUERIES,
            'max_results': config.MAX_RESULTS,
            'sec_holdings_enabled': config.SEC_INCLUDE_HOLDINGS,
        },
        'topics': [{
            **records.get(topic, {}),
            'topic': topic,
            'summary': summary,
        } for topic, summary in summaries.items()],
    }
    markdown_path, json_path = Path(str(base) + '.md'), Path(str(base) + '.json')
    atomic_write(markdown_path, markdown)
    atomic_write(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    return str(json_path)


def record_render_status(archive_path, status, error_type=''):
    """Update the companion archive without exposing renderer output or stack traces."""
    try:
        path = Path(archive_path)
        payload = json.loads(path.read_text(encoding='utf-8'))
        payload['report_status'] = {'pdf': status, 'error_type': error_type}
        atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    except (OSError, ValueError, TypeError) as exc:
        print(f'⚠️ PDF 状态留档失败: {type(exc).__name__}')


def load_report_archive(path):
    """Read a report snapshot for offline use; reject ambiguous or oversized inputs."""
    path = Path(path)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('Report archive exceeds 32 MiB')
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or type(payload.get('schema_version')) is not int or payload['schema_version'] != 1:
        raise ValueError('Unsupported report archive schema')
    if not isinstance(payload.get('title'), str) or not payload['title'].strip():
        raise ValueError('Report archive requires a title')
    if 'generated_at' in payload and not isinstance(payload['generated_at'], str):
        raise ValueError('Archived generated_at must be text')
    if 'settings' in payload and not isinstance(payload['settings'], dict):
        raise ValueError('Archived settings must be an object')
    topics = payload.get('topics')
    if not isinstance(topics, list) or not topics or len(topics) > 1000:
        raise ValueError('Report archive requires 1–1000 topics')
    seen = set()
    for record in topics:
        if (not isinstance(record, dict) or not isinstance(record.get('topic'), str) or
                not record['topic'].strip() or not isinstance(record.get('summary'), str)):
            raise ValueError('Each archived topic requires a name and text summary')
        if record['topic'] in seen:
            raise ValueError('Report archive contains duplicate topic names')
        seen.add(record['topic'])
        if 'raw_evidence' in record and not isinstance(record['raw_evidence'], str):
            raise ValueError('Archived raw_evidence must be text')
        if 'status' in record and not isinstance(record['status'], str):
            raise ValueError('Archived topic status must be text')
    return payload


def recovery_notice(payload):
    recovery = payload.get('recovery')
    if not isinstance(recovery, dict):
        return ''
    if recovery.get('expected_topics_known') is not True:
        return '检查点恢复说明：仅汇总已保存主题；旧批次未记录预期主题清单，无法确认原批次是否完整。本次未重新检索。'
    missing = recovery.get('missing_topics', [])
    count = len(missing) if isinstance(missing, list) else 0
    return f'检查点恢复说明：按原批次清单恢复；尚未完成 {count} 项，已在表中单独标注。本次未重新检索。'


def render_report_archive(archive_path, output_dir=None):
    """Re-render stored summaries without constructing search, model or email services."""
    from .report_generator import ReportGenerator

    source = Path(archive_path)
    payload = load_report_archive(source)
    directory = Path(output_dir) if output_dir else source.parent
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    target = directory / f'{source.stem}_replay_{stamp}.pdf'
    summaries = {record['topic']: record['summary'] for record in payload['topics']}
    records = {record['topic']: record for record in payload['topics']}
    overview = coverage_summary(summaries, records)
    notice = recovery_notice(payload)
    if notice:
        overview = notice + '\n\n' + overview
    markdown = report_markdown(payload['title'], summaries, records, overview)
    replay = {**payload, 'report_status': {'pdf': 'pending'}, 'replay': {
        'source_archive': source.name,
        'rendered_at': datetime.now(timezone.utc).isoformat(),
        'network_used': False,
    }}
    # Keep the original evidence timestamp and settings: replay is not new research.
    atomic_write(target.with_suffix('.md'), markdown)
    atomic_write(target.with_suffix('.json'), json.dumps(replay, ensure_ascii=False, indent=2) + '\n')
    original_time = payload.get('generated_at')
    try:
        ReportGenerator(str(target), topic=payload['title'],
                        source_generated_at=original_time if isinstance(original_time, str) else '未知').create_pdf(
                            summaries, overview=overview,
                            topic_notices={topic: topic_notice(record) for topic, record in records.items()},
                            source_urls={topic: [item['url'] for item in evidence_index(record.get('raw_evidence', ''))]
                                         for topic, record in records.items()})
    except BaseException as exc:
        record_render_status(target.with_suffix('.json'), 'failed', type(exc).__name__)
        raise
    record_render_status(target.with_suffix('.json'), 'completed')
    return str(target)
