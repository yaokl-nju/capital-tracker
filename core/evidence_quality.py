"""Describe evidence coverage, without claiming that source facts were verified."""
import re
from collections import Counter
from urllib.parse import urlparse

from core.search_service import canonical_url, published_date


STATUS_LABELS = {'analyzed': '已生成分析', 'evidence_only': '仅原始证据',
                 'no_evidence': '未找到有效证据', 'error': '处理失败', 'not_completed': '尚未完成'}


def evidence_index(raw_data):
    """Index the exact bounded model input, including distinct baseline documents."""
    records = []
    for block in raw_data.split('\n---\n'):
        fields = {}
        for name in ('时间', '信源', '标题', '链接', '检索渠道'):
            match = re.search(r'^' + name + r': (.*)$', block, re.M)
            fields[name] = match[1].strip() if match else ''
        if not canonical_url(fields['链接']):
            continue
        records.append({
            'id': f'E{len(records) + 1:03d}',
            'url': fields['链接'], 'title': fields['标题'], 'source': fields['信源'],
            'date': fields['时间'], 'backend': fields['检索渠道'],
            'date_available': published_date(fields['时间']) is not None,
            'historical_baseline': block.startswith('证据用途: 历史比较基线'),
            'title_only': '仅标题，未读取正文' in block,
        })
    return records


def evidence_quality(index):
    current = [item for item in index if not item['historical_baseline']]
    return {
        'evidence_fragments': len(index),
        'unique_sources': len({canonical_url(item['url']) for item in index}),
        'source_domains': sorted({urlparse(item['url']).hostname for item in index}),
        'current_fragments': len(current),
        'dated_fragments': sum(item['date_available'] for item in current),
        'undated_fragments': sum(not item['date_available'] for item in current),
        'historical_baselines': len(index) - len(current),
        'title_only_fragments': sum(item['title_only'] for item in current),
    }


def coverage_summary(summaries, records):
    """Human-readable coverage table kept separate from the model's analysis."""
    if not any(topic in records for topic in summaries):
        return ''
    lines = ['资料覆盖说明：以下统计描述本次输入，不代表逐项事实核验；未找到证据不等于没有资本事件。', '',
             '| 机构 | 处理状态 | 来源链接 | 带日期 | 日期未知 | 历史基线 |',
             '| --- | --- | ---: | ---: | ---: | ---: |']
    for topic in summaries:
        record = records.get(topic, {})
        index = evidence_index(record.get('raw_evidence', ''))
        quality = evidence_quality(index)
        label = STATUS_LABELS.get(record.get('status'), '未记录')
        safe_topic = re.sub(r'[\r\n]', ' ', topic).replace('|', '\\|')
        lines.append(f"| {safe_topic} | {label} | {quality['unique_sources']} | "
                     f"{quality['dated_fragments']} | {quality['undated_fragments']} | "
                     f"{quality['historical_baselines']} |")
    statuses = Counter(records.get(topic, {}).get('status', 'unknown') for topic in summaries)
    lines += ['', '来源链接按规范化 URL 去重；日期列按证据片段统计，不含历史比较基线。', '',
              '处理分布：' + '；'.join(f'{STATUS_LABELS.get(status, "未记录")} {count} 项'
                                       for status, count in statuses.items()) + '。']
    return '\n'.join(lines)


def topic_notice(record):
    """Keep input coverage visible when readers jump directly to a topic bookmark."""
    quality = evidence_quality(evidence_index(record.get('raw_evidence', '')))
    label = STATUS_LABELS.get(record.get('status'), '未记录')
    return (f"程序统计：{label}；输入 {quality['unique_sources']} 个来源链接，"
            f"带日期 {quality['dated_fragments']} 片段、日期未知 {quality['undated_fragments']} 片段，"
            f"历史基线 {quality['historical_baselines']} 片段。带日期或链接匹配不表示已经核验原文事实。")
