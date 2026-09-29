"""Bounded SEC 13F XML snapshots; no inference of trade times or total AUM."""
import re
from datetime import date, datetime
from xml.etree import ElementTree


MAX_XML_BYTES = 5_000_000


def parse_xml(content, root_name):
    if not isinstance(content, bytes) or len(content) > MAX_XML_BYTES:
        raise ValueError('Invalid or oversized XML')
    upper = content.upper()
    if b'<!DOCTYPE' in upper or b'<!ENTITY' in upper or b'\x00' in content:
        raise ValueError('XML declarations or unsupported encoding')
    root = ElementTree.fromstring(content)
    if root.tag.rsplit('}', 1)[-1] != root_name:
        raise ValueError('Unexpected XML root')
    return root


def field(node, name, default=''):
    element = node.find('.//{*}' + name)
    return (element.text or '').strip() if element is not None else default


def integer(value):
    if not re.fullmatch(r'\d{1,20}', value):
        raise ValueError('Invalid SEC numeric field')
    return int(value)


def parse_snapshot(cover_bytes, table_bytes, filing_date, expected_cik):
    cover = parse_xml(cover_bytes, 'edgarSubmission')
    table = parse_xml(table_bytes, 'informationTable')
    if integer(field(cover, 'cik')) != int(expected_cik):
        raise ValueError('Unexpected filer CIK')
    if field(cover, 'submissionType') not in ('13F-HR', '13F-HR/A'):
        raise ValueError('Not a holdings report')
    period = datetime.strptime(field(cover, 'reportCalendarOrQuarter'), '%m-%d-%Y').date()
    filed = date.fromisoformat(filing_date)
    if period > filed:
        raise ValueError('Future report period')
    # SEC switched the submitted market-value unit on 2023-01-03.
    multiplier = 1 if filed >= date(2023, 1, 3) else 1000
    rows = []
    for entry in table.findall('.//{*}infoTable'):
        issuer, security = field(entry, 'nameOfIssuer'), field(entry, 'cusip')
        share_type, put_call = field(entry, 'sshPrnamtType'), field(entry, 'putCall')
        if not issuer or not re.fullmatch(r'[A-Za-z0-9*@#]{9}', security):
            raise ValueError('Invalid issuer or CUSIP')
        if share_type not in ('SH', 'PRN') or put_call not in ('', 'Put', 'Call', 'PUT', 'CALL'):
            raise ValueError('Unknown security type')
        rows.append({
            'issuer': issuer, 'class': field(entry, 'titleOfClass'), 'cusip': security.upper(),
            'value_usd': integer(field(entry, 'value')) * multiplier,
            'shares_or_principal': integer(field(entry, 'sshPrnamt')),
            'share_type': share_type, 'put_call': put_call.upper(),
        })
    declared_entries = integer(field(cover, 'tableEntryTotal'))
    declared_total = integer(field(cover, 'tableValueTotal')) * multiplier
    total = sum(row['value_usd'] for row in rows)
    # Independently rounded entries can differ slightly from the rounded cover total.
    if len(rows) != declared_entries or abs(total - declared_total) > max(1, len(rows)) * multiplier:
        raise ValueError('Incomplete information table or inconsistent totals')
    amendment = field(cover, 'isAmendment', 'false').lower() in ('true', '1')
    amendment_type = field(cover, 'amendmentType').upper()
    if field(cover, 'submissionType').endswith('/A') and not amendment:
        raise ValueError('Inconsistent amendment flag')
    return {
        'period': period.isoformat(), 'filing_date': filing_date,
        'manager': field(cover, 'name'), 'cik': f'{int(expected_cik):010d}',
        'report_type': field(cover, 'reportType'),
        'total_value_usd': total, 'declared_total_usd': declared_total, 'rows': rows,
        'amendment_type': amendment_type if amendment else '',
        'complete': (not amendment or amendment_type == 'RESTATEMENT') and
                    field(cover, 'isConfidentialOmitted', 'false').lower() not in ('true', '1'),
    }


def normalized_class(value):
    words = re.sub(r'[^A-Z0-9]+', ' ', value.upper()).strip().split()
    aliases = {'SPONSORED': 'SPON', 'CLASS': 'CL'}
    return ' '.join(aliases.get(word, word) for word in words)


def grouped_positions(snapshot):
    positions = {}
    for row in snapshot['rows']:
        key = (row['cusip'], normalized_class(row['class']), row['share_type'], row['put_call'])
        if key not in positions:
            positions[key] = dict(row)
        else:
            positions[key]['value_usd'] += row['value_usd']
            positions[key]['shares_or_principal'] += row['shares_or_principal']
    return positions


def compare_snapshots(current, previous):
    """Compare consecutive complete quarter-end snapshots, never market values as trades."""
    if not current['complete'] or not previous['complete'] or current['cik'] != previous['cik']:
        return {}, '修正申报、保密省略或主体不一致，未进行完整增减持比较'
    if 'COMBINATION' in current.get('report_type', '') or 'COMBINATION' in previous.get('report_type', ''):
        return {}, '组合申报未覆盖主体全部持仓，未进行完整增减持比较'
    current_date, prior_date = date.fromisoformat(current['period']), date.fromisoformat(previous['period'])
    quarter_ends = {(3, 31), (6, 30), (9, 30), (12, 31)}
    if ((current_date.month, current_date.day) not in quarter_ends or
            (prior_date.month, prior_date.day) not in quarter_ends or
            not 89 <= (current_date - prior_date).days <= 92):
        return {}, '缺少相邻季度末可比快照，未进行增减持比较'
    new, old = grouped_positions(current), grouped_positions(previous)
    changes = {}
    for key, row in new.items():
        prior = old.get(key)
        if prior is None and any(old_key[0] == key[0] and old_key[2:] == key[2:] for old_key in old):
            changes[key] = {'change': '同一 CUSIP 的类别描述变化，数量变动未核实', 'quantity_delta': None}
            continue
        shares = row['shares_or_principal']
        delta = shares - prior['shares_or_principal'] if prior else shares
        changes[key] = {
            'change': '本期首次披露该类别' if prior is None else '数量增加' if delta > 0 else '数量减少' if delta < 0 else '数量未变',
            'quantity_delta': delta,
        }
    for key, row in old.items():
        # A changed class description is not evidence that the old position disappeared.
        if key not in new and not any(new_key[0] == key[0] and new_key[2:] == key[2:] for new_key in new):
            changes[key] = {'change': '本期表内未再披露该类别',
                            'quantity_delta': -row['shares_or_principal']}
    return changes, '数量变化为申报快照对照，可能受拆股、合并及修正影响，不证明具体买卖或交易时间'


def format_snapshot(snapshot, previous=None, limit=10):
    changes, note = compare_snapshots(snapshot, previous) if previous else ({}, '未取得上期可比信息表，不确认新建仓或增减持')
    positions = grouped_positions(snapshot)
    selected = sorted(positions.items(), key=lambda entry: entry[1]['value_usd'], reverse=True)[:limit]
    lines = [f'SEC 13F 持仓信息表；主体 {snapshot["manager"]}，CIK {snapshot["cik"]}；'
             f'快照报告期 {snapshot["period"]}，申报日期 {snapshot["filing_date"]}。',
             f'信息表合计 USD {snapshot["total_value_usd"]:,}；只代表本表申报范围，不是机构总资产。',
             f'按申报市值显示前 {len(selected)} 项，共 {len(positions)} 项；未展示项不等于清仓。{note}。']
    if previous:
        lines.append(f'数量比较基准：上期报告期 {previous["period"]}，申报日期 {previous["filing_date"]}；'
                     '比较使用两期完整信息表，展示条数限制不参与数量差计算。')
    if snapshot['total_value_usd'] != snapshot.get('declared_total_usd', snapshot['total_value_usd']):
        lines.append(f'信息表逐项合计与封面声明合计差 USD '
                     f'{snapshot["total_value_usd"] - snapshot["declared_total_usd"]:+,}，'
                     '差异在逐项舍入容差内；本表比例按逐项合计计算，仍以原始申报为准。')
    if not snapshot['complete']:
        lines.append('本表含补充修正或保密省略，不能视为完整持仓；不计算持仓比例。')
    if 'COMBINATION' in snapshot.get('report_type', ''):
        lines.append('这是组合申报，部分持仓由其他管理人申报；仅展示本表范围，不是主体完整持仓。')
    if any(row['put_call'] for row in snapshot['rows']):
        lines.append('PUT/CALL 为期权申报；数量及多数栏位描述标的证券，不能把申报数量当作期权合约数或现货持仓。')
    for key, row in selected:
        change = changes.get(key, {}).get('change', '变动未核实')
        delta = changes.get(key, {}).get('quantity_delta')
        ratio = (f'{row["value_usd"] / snapshot["total_value_usd"]:.2%}'
                 if snapshot['complete'] and snapshot['total_value_usd'] else '未计算')
        lines.append(f'{row["issuer"]} / {row["class"]} / CUSIP {row["cusip"]} / '
                     f'{row["put_call"] or "非期权"}；USD {row["value_usd"]:,}；'
                     f'数量 {row["shares_or_principal"]:,} {row["share_type"]}；'
                     f'占本申报表市值 {ratio}；{change}' + (f'（数量差 {delta:+,}）' if delta is not None else '') + '。')
    selected_keys = {key for key, _ in selected}
    additional = [(key, row) for key, row in sorted(positions.items(),
                  key=lambda entry: entry[1]['value_usd'], reverse=True)
                  if key not in selected_keys and changes.get(key, {}).get('quantity_delta', 0) is not None
                  and changes.get(key, {}).get('quantity_delta', 0) > 0][:3]
    if additional:
        lines.append('前列持仓以外的新披露或数量增加线索（最多 3 项，按本期申报市值排序）：')
        for key, row in additional:
            change = changes[key]
            lines.append(f'{row["issuer"]} / {row["class"]} / CUSIP {row["cusip"]} / '
                         f'{row["put_call"] or "非期权"}；USD {row["value_usd"]:,}；'
                         f'数量 {row["shares_or_principal"]:,} {row["share_type"]}；'
                         f'{change["change"]}（数量差 {change["quantity_delta"]:+,}）。')
    if previous:
        missing = [(key, row) for key, row in sorted(grouped_positions(previous).items(),
                   key=lambda entry: entry[1]['value_usd'], reverse=True)
                   if changes.get(key, {}).get('change') == '本期表内未再披露该类别'][:3]
        if missing:
            lines.append('上期披露、本期表内未再披露线索（最多 3 项，按上期申报市值排序；不证明实际卖出或清仓）：')
            for key, row in missing:
                lines.append(f'{row["issuer"]} / {row["class"]} / CUSIP {row["cusip"]} / '
                             f'{row["put_call"] or "非期权"}；上期数量 '
                             f'{row["shares_or_principal"]:,} {row["share_type"]}。')
    lines.append('报告期为季度末快照；不据此推断买入时间、投资理由、增长催化剂或保证收益。')
    return '\n'.join(lines)


def load_snapshot(get, document_url, filing_date, expected_cik):
    """Read only safe XML filenames from one verified SEC accession directory."""
    match = re.fullmatch(r'https://www\.sec\.gov/Archives/edgar/data/(\d{1,10})/(\d{18})/'
                         r'(?:[A-Za-z0-9_-]+/)*([A-Za-z0-9_.-]+\.xml)', document_url, re.I)
    if not match or int(match[1]) != int(expected_cik) or '..' in match[3]:
        raise ValueError('Not a verified SEC XML filing URL')
    base = f'https://www.sec.gov/Archives/edgar/data/{match[1]}/{match[2]}/'
    listing = get(base + 'index.json').json()['directory']['item']
    if not isinstance(listing, list):
        raise ValueError('Invalid accession index')
    candidates = []
    for item in listing:
        if not isinstance(item, dict):
            continue
        name = item.get('name', '')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.xml', name, re.I) or '..' in name:
            continue
        size = item.get('size')
        if size and (not str(size).isdigit() or int(size) > MAX_XML_BYTES):
            continue
        candidates.append(name)
    if match[3] not in candidates:
        raise ValueError('Cover XML is absent from accession directory')
    cover = get(base + match[3]).content
    for name in [name for name in candidates if name != match[3]][:4]:
        content = get(base + name).content
        try:
            parse_xml(content, 'informationTable')
        except (ValueError, ElementTree.ParseError):
            continue
        snapshot = parse_snapshot(cover, content, filing_date, expected_cik)
        snapshot['source_url'] = base + name
        return snapshot
    raise ValueError('No supported information table XML')


def enrich_filings(get, results, history, cik, limit=10):
    """Enrich current metadata in place while retaining an independently citable baseline."""
    def has_period(item):
        try:
            date.fromisoformat(item.get('_report_period', ''))
            return True
        except (TypeError, ValueError):
            return False
    candidates = [item for item in results if item['_filing_type'] in ('13F-HR', '13F-HR/A')
                  and has_period(item)]
    if not candidates:
        return
    item = max(candidates, key=lambda r: (r['_report_period'], r['date']))
    try:
        current = load_snapshot(get, item['href'], item['date'], cik)
        if current['period'] != item['_report_period']:
            raise ValueError('Submission and XML periods do not match')
        prior_items = [r for r in history if r['_filing_type'] in ('13F-HR', '13F-HR/A')
                       and has_period(r) and r['_report_period'] < current['period']]
        previous = None
        if prior_items:
            prior = max(prior_items, key=lambda r: (r['_report_period'], r['date']))
            try:
                previous = load_snapshot(get, prior['href'], prior['date'], cik)
                if previous['period'] != prior['_report_period']:
                    previous = None
            except Exception as exc:
                print(f' ⚠️ SEC 上期信息表不可用: {type(exc).__name__}')
        item['body'] = format_snapshot(current, previous, limit)
        item['href'] = current['source_url']
        item['_holdings_count'] = len(current['rows'])
        if previous:
            results.append({
                'title': f'{previous["manager"]} — 上期 13F 对照基线 — {previous["period"]}',
                'href': previous['source_url'], 'date': previous['filing_date'], 'source': 'SEC.gov',
                '_filing_type': prior['_filing_type'], '_baseline': True,
                'body': '历史快照，仅作为本期数量比较基线，不作为近期新闻或交易记录。\n' +
                        format_snapshot(previous, limit=limit),
            })
    except Exception as exc:
        print(f' ⚠️ SEC 持仓 XML 不可用，保留申报元数据: {type(exc).__name__}')
