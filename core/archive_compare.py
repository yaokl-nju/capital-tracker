"""Offline comparisons of retrieved sources; never infer trades from search changes."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import atomic_write, load_report_archive
from .evidence_quality import evidence_index, STATUS_LABELS
from .search_service import canonical_url


def _source_map(record):
    sources = {}
    for block in record.get('raw_evidence', '').split('\n---\n'):
        index = evidence_index(block)
        if not index:
            continue
        source = index[0]
        key = canonical_url(source['url'])
        # Ignore volatile retrieval-channel labels, but preserve date and content changes.
        content = re.sub(r'^检索渠道: .*\n', '', block, flags=re.M)
        content = re.sub(r'^链接: .*$', lambda match: '链接: ' + key, content, flags=re.M)
        content = re.sub(r'\s+', ' ', content).strip()
        fingerprint = hashlib.sha256(content.encode()).hexdigest()
        if key in sources:
            sources[key]['fingerprints'].add(fingerprint)
        else:
            sources[key] = {**source, 'fingerprints': {fingerprint}}
    return sources


def compare_archives(before, after):
    old = {r['topic']: r for r in before['topics']}
    new = {r['topic']: r for r in after['topics']}
    topics = list(dict.fromkeys([*new, *old]))
    result = []
    for topic in topics:
        prior, current = _source_map(old.get(topic, {})), _source_map(new.get(topic, {}))
        added = [key for key in current if key not in prior]
        removed = [key for key in prior if key not in current]
        changed = [key for key in current if key in prior and
                   current[key]['fingerprints'] != prior[key]['fingerprints']]
        def details(mapping, keys):
            return [{k: v for k, v in mapping[key].items() if k not in ('fingerprints', 'id')} for key in keys]
        result.append({
            'topic': topic,
            'topic_state': 'added' if topic not in old else 'removed' if topic not in new else 'retained',
            'before_status': old.get(topic, {}).get('status', 'unknown'),
            'after_status': new.get(topic, {}).get('status', 'unknown'),
            'analysis_changed': old.get(topic, {}).get('summary') != new.get(topic, {}).get('summary'),
            'added_sources': details(current, added), 'removed_sources': details(prior, removed),
            'changed_sources': details(current, changed),
            'unchanged_source_count': len(current) - len(added) - len(changed),
        })
    return result


def _label(value):
    text = re.sub(r'[\r\n]', ' ', value)
    return re.sub(r'([\\`*_{}\[\]<>|])', r'\\\1', text)


def comparison_markdown(topics, before_name, after_name):
    lines = ['# 检索来源变化对照', '', f'对照档案：{_label(before_name)} → {_label(after_name)}', '',
             '本对照仅比较两次保存的检索来源与摘要。新增来源不等于新发生投资；未再收录不等于事件消失、卖出或清仓。', '',
             '| 机构 | 新增来源 | 摘要或日期有变化 | 本次未再收录 | 未变来源 |',
             '| --- | ---: | ---: | ---: | ---: |']
    for row in topics:
        lines.append(f"| {_label(row['topic'])} | {len(row['added_sources'])} | {len(row['changed_sources'])} | "
                     f"{len(row['removed_sources'])} | {row['unchanged_source_count']} |")
    for row in topics:
        lines += ['', f"## {_label(row['topic'])}", '',
                  f"处理状态：{STATUS_LABELS.get(row['before_status'], '未记录')} → "
                  f"{STATUS_LABELS.get(row['after_status'], '未记录')}；分析文本{'有变化' if row['analysis_changed'] else '未变'}。"]
        for key, heading in [('added_sources', '新增检索来源'), ('changed_sources', '同链接摘要或日期变化'),
                             ('removed_sources', '本次检索未再收录')]:
            if not row[key]:
                continue
            lines += ['', f'### {heading}', '']
            for source in row[key]:
                # Angle destinations handle SEC links with nested parentheses safely.
                url = source['url'].replace('<', '%3C').replace('>', '%3E')
                flags = '；历史比较基线' if source['historical_baseline'] else ''
                lines.append(f"- [{_label(source['title'] or source['source'] or '来源')}](<{url}>)"
                             f" — {_label(source['date'] or '日期未知')}{flags}")
    return '\n'.join(lines) + '\n'


def save_archive_comparison(before_path, after_path, output_dir=None):
    before_path, after_path = Path(before_path), Path(after_path)
    before, after = load_report_archive(before_path), load_report_archive(after_path)
    topics = compare_archives(before, after)
    directory = Path(output_dir) if output_dir else after_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    base = directory / f'Source_Comparison_{stamp}'
    payload = {'schema_version': 1, 'kind': 'source_comparison',
               'generated_at': datetime.now(timezone.utc).isoformat(),
               'before': {'archive': before_path.name, 'generated_at': before.get('generated_at')},
               'after': {'archive': after_path.name, 'generated_at': after.get('generated_at')},
               'topics': topics}
    atomic_write(base.with_suffix('.json'), json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    atomic_write(base.with_suffix('.md'), comparison_markdown(topics, before_path.name, after_path.name))
    return str(base.with_suffix('.md'))
