"""Offline assembly of one interrupted batch, preserving completion and evidence times."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import atomic_write, load_report_archive, render_report_archive


MAX_RECOVERY_BYTES = 32 * 1024 * 1024


def topic_filename(topic):
    return 'topic_' + hashlib.sha256(topic.encode('utf-8')).hexdigest()[:24] + '.json'


def save_batch_manifest(directory, title, topics):
    payload = {'schema_version': 1, 'kind': 'topic_checkpoint_batch', 'title': title,
               'generated_at': datetime.now(timezone.utc).isoformat(), 'topics': topics}
    atomic_write(Path(directory) / 'batch.json', json.dumps(payload, ensure_ascii=False, indent=2) + '\n')


def assemble_checkpoint_archive(directory, output_dir=None):
    """Create a new archive from completed checkpoint files; never restart research."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError('Checkpoint recovery requires one batch directory')
    paths = sorted(directory.glob('topic_*.json'))
    if not paths or len(paths) > 1000:
        raise ValueError('Checkpoint batch requires 1–1000 saved topic files')
    if sum(path.stat().st_size for path in paths) > MAX_RECOVERY_BYTES:
        raise ValueError('Checkpoint batch exceeds 32 MiB')
    records, settings, title = {}, None, None
    for path in paths:
        payload = load_report_archive(path)
        if len(payload['topics']) != 1:
            raise ValueError('Each checkpoint must contain exactly one topic')
        record = payload['topics'][0]
        name = record['topic']
        if path.name != topic_filename(name) or name in records:
            raise ValueError('Checkpoint filename or topic identity is inconsistent')
        current_settings = payload.get('settings', {})
        if settings is None:
            settings, title = current_settings, payload['title']
        elif settings != current_settings or title != payload['title']:
            raise ValueError('Checkpoints have different titles or research settings')
        records[name] = {**record, 'checkpoint_generated_at': payload.get('generated_at')}
    manifest_path = directory / 'batch.json'
    missing = []
    if manifest_path.exists():
        if manifest_path.stat().st_size > 128 * 1024:
            raise ValueError('Checkpoint manifest exceeds 128 KiB')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if (not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int or
                manifest['schema_version'] != 1 or manifest.get('kind') != 'topic_checkpoint_batch' or
                not isinstance(manifest.get('title'), str) or not manifest['title'].strip() or
                not isinstance(manifest.get('generated_at'), str) or not manifest['generated_at']):
            raise ValueError('Invalid checkpoint manifest')
        order = manifest.get('topics')
        if (not isinstance(order, list) or not 1 <= len(order) <= 1000 or
                any(not isinstance(t, str) or not t.strip() for t in order) or len(set(order)) != len(order)):
            raise ValueError('Invalid checkpoint topic list')
        if set(records) - set(order):
            raise ValueError('Saved topic is absent from the checkpoint manifest')
        if title != manifest['title'] + '（单主题检查点）':
            raise ValueError('Checkpoint title does not match batch manifest')
        title = manifest['title']
        generated_at = manifest.get('generated_at')
        missing = [topic for topic in order if topic not in records]
        for topic in missing:
            records[topic] = {'topic': topic, 'summary': '该主题尚未完成，未保存研究结果；本次离线恢复未重新检索。',
                              'raw_evidence': '', 'status': 'not_completed'}
    else:
        # Older batches lack an intended topic list: do not claim completeness.
        order = list(records)
        title = title.removesuffix('（单主题检查点）')
        generated_at = min((r.get('checkpoint_generated_at') or '' for r in records.values()), default='')
    default_dir = directory.parent.parent if directory.parent.name == '.checkpoints' else directory.parent
    target_dir = Path(output_dir) if output_dir else default_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    target = target_dir / f'Checkpoint_Recovery_{stamp}.json'
    payload = {'schema_version': 1, 'title': title + '（检查点恢复）', 'generated_at': generated_at,
               'settings': settings, 'report_status': {'pdf': 'not_requested'},
               'recovery': {'batch': directory.name, 'assembled_at': datetime.now(timezone.utc).isoformat(),
                            'saved_topic_count': len(paths), 'missing_topics': missing,
                            'expected_topics_known': manifest_path.exists(), 'network_used': False},
               'topics': [records[topic] for topic in order]}
    atomic_write(target, json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    return str(target)


def recover_checkpoint_report(directory, output_dir=None):
    archive = assemble_checkpoint_archive(directory, output_dir)
    return render_report_archive(archive, output_dir)
