"""Small, atomic report sidecars for inspecting the evidence behind a PDF."""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


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


def save_report_artifacts(pdf_path, title, summaries, records, config):
    """Persist analysis and input evidence, never serializing configuration secrets."""
    base = Path(pdf_path).with_suffix('')
    markdown = f'# {title}\n\n' + '\n\n'.join(
        f'## {topic}\n\n{summary}' for topic, summary in summaries.items()) + '\n'
    payload = {
        'schema_version': 1,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'title': title,
        'settings': {
            'model': config.DEEPSEEK.model,
            'search_backends': list(config.SEARCH_BACKENDS),
            'news_timelimit': config.NEWS_TIMELIMIT,
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
