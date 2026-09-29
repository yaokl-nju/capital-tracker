from pathlib import Path
from unittest.mock import Mock

import pytest

from core.sec_holdings import parse_snapshot, format_snapshot, compare_snapshots, load_snapshot, parse_xml
from core.search_service import SecEdgarSearcher, SearchService, SearchAggregator
from config.settings import Config


def cover(period='06-30-2026', entries=1, total=100, amendment='', confidential='false', cik='1709323'):
    amendment_xml = f'<isAmendment>true</isAmendment><amendmentType>{amendment}</amendmentType>' if amendment else ''
    form = '13F-HR/A' if amendment else '13F-HR'
    return f'''<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">
        <submissionType>{form}</submissionType><cik>{cik}</cik>
        <reportCalendarOrQuarter>{period}</reportCalendarOrQuarter><name>Test Manager</name>
        {amendment_xml}<tableEntryTotal>{entries}</tableEntryTotal><tableValueTotal>{total}</tableValueTotal>
        <isConfidentialOmitted>{confidential}</isConfidentialOmitted></edgarSubmission>'''.encode()


def table(rows=None):
    rows = rows or [('Issuer', 'COM', '123456789', 100, 10, 'SH', '')]
    body = ''.join(f'<infoTable><nameOfIssuer>{issuer}</nameOfIssuer><titleOfClass>{cls}</titleOfClass>'
                   f'<cusip>{cusip}</cusip><value>{value}</value><sshPrnamt>{shares}</sshPrnamt>'
                   f'<sshPrnamtType>{kind}</sshPrnamtType><putCall>{option}</putCall></infoTable>'
                   for issuer, cls, cusip, value, shares, kind, option in rows)
    return f'<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">{body}</informationTable>'.encode()


def snapshot(period='06-30-2026', shares=10, value=100, **kwargs):
    return parse_snapshot(cover(period=period, total=value, **kwargs),
                          table([('Issuer', 'COM', '123456789', value, shares, 'SH', '')]),
                          '2026-08-14', '1709323')


def test_real_sec_fixture_totals_share_classes_and_rounding():
    xml = Path(__file__).with_name('fixtures').joinpath('himalaya-2026q2-information-table.xml').read_bytes()
    result = parse_snapshot(cover(entries=8, total=3702921098), xml, '2026-08-14', '1709323')
    assert len(result['rows']) == 8 and result['total_value_usd'] == 3702921099
    alphabets = [row for row in result['rows'] if row['issuer'] == 'ALPHABET INC']
    assert len(alphabets) == 2 and len({row['cusip'] for row in alphabets}) == 2
    text = format_snapshot(result, limit=2)
    assert '差 USD +1' in text and '前 2 项，共 8 项' in text
    assert '不是机构总资产' in text and '未展示项不等于清仓' in text


@pytest.mark.parametrize('filed,multiplier', [('2023-01-02', 1000), ('2023-01-03', 1)])
def test_value_unit_switch_uses_filing_date(filed, multiplier):
    result = parse_snapshot(cover(period='12-31-2022'), table(), filed, '1709323')
    assert result['total_value_usd'] == 100 * multiplier


def test_quarter_comparison_across_value_unit_switch_does_not_invent_quantity_changes():
    previous = parse_snapshot(cover(period='09-30-2022'), table(), '2022-11-15', '1709323')
    current = parse_snapshot(cover(period='12-31-2022', total=100000),
                             table([('Issuer', 'COM', '123456789', 100000, 10, 'SH', '')]),
                             '2023-02-15', '1709323')
    assert current['total_value_usd'] == previous['total_value_usd'] == 100000
    assert next(iter(compare_snapshots(current, previous)[0].values()))['quantity_delta'] == 0


@pytest.mark.parametrize('current_period,prior_period,comparable', [
    ('2024-03-31', '2023-12-31', True),
    ('2025-03-31', '2024-12-31', True),
    ('2026-12-31', '2026-09-30', True),
    ('2026-06-30', '2026-03-30', False),
    ('2026-03-31', '2026-06-30', False),
])
def test_comparison_handles_leap_years_year_boundaries_and_reversed_periods(current_period, prior_period, comparable):
    current, previous = snapshot(shares=20), snapshot('03-31-2026')
    current['period'], previous['period'] = current_period, prior_period
    assert bool(compare_snapshots(current, previous)[0]) is comparable


@pytest.mark.parametrize('kwargs', [{'cik': '1'}, {'entries': 2}, {'total': 200},
                                  {'period': '12-31-2026'}])
def test_invalid_identity_incomplete_tables_and_bad_totals_rejected(kwargs):
    with pytest.raises(ValueError):
        parse_snapshot(cover(**kwargs), table(), '2026-08-14', '1709323')


@pytest.mark.parametrize('xml', [b'<!DOCTYPE informationTable [<!ENTITY x "boom">]><informationTable/>',
                               b'<wrong/>', b'<informationTable>\x00</informationTable>', b'x' * 5_000_001])
def test_unsafe_wrong_or_oversized_xml_rejected(xml):
    with pytest.raises(ValueError):
        parse_xml(xml, 'informationTable')


@pytest.mark.parametrize('value', ['-1', 'NaN', '1e10', 'Infinity', '10.5'])
def test_invalid_numeric_fields_fail_closed(value):
    with pytest.raises(ValueError):
        parse_snapshot(cover(), table([('Issuer', 'COM', '123456789', value, 10, 'SH', '')]),
                       '2026-08-14', '1709323')


@pytest.mark.parametrize('issuer,cusip,kind,option', [
    ('', '123456789', 'SH', ''),
    ('Issuer', 'SHORT', 'SH', ''),
    ('Issuer', '123456789', 'UNKNOWN', ''),
    ('Issuer', '123456789', 'SH', 'FUTURE'),
])
def test_invalid_security_identity_and_quantity_types_reject_the_snapshot(issuer, cusip, kind, option):
    with pytest.raises(ValueError):
        parse_snapshot(cover(), table([(issuer, 'COM', cusip, 100, 10, kind, option)]),
                       '2026-08-14', '1709323')


def test_wrong_form_and_inconsistent_amendment_metadata_are_rejected():
    with pytest.raises(ValueError, match='Not a holdings'):
        parse_snapshot(cover().replace(b'13F-HR', b'13F-NT'), table(), '2026-08-14', '1709323')
    with pytest.raises(ValueError, match='amendment flag'):
        parse_snapshot(cover().replace(b'13F-HR', b'13F-HR/A'), table(), '2026-08-14', '1709323')


def test_comparison_uses_quantity_not_market_value_and_does_not_claim_trades():
    old, new = snapshot('03-31-2026', shares=10, value=100), snapshot(shares=10, value=200)
    changes, note = compare_snapshots(new, old)
    assert next(iter(changes.values())) == {'change': '数量未变', 'quantity_delta': 0}
    assert '不证明具体买卖' in note
    new = snapshot(shares=15)
    assert next(iter(compare_snapshots(new, old)[0].values()))['quantity_delta'] == 5


@pytest.mark.parametrize('kwargs', [{'amendment': 'NEW HOLDINGS'}, {'confidential': 'true'}])
def test_incomplete_snapshot_disables_changes_and_percentages(kwargs):
    current = snapshot(**kwargs)
    assert not compare_snapshots(current, snapshot('03-31-2026'))[0]
    text = format_snapshot(current)
    assert '不计算持仓比例' in text and '占本申报表市值 未计算' in text


def test_restatement_can_be_compared_but_nonadjacent_quarters_cannot():
    current = snapshot(amendment='RESTATEMENT')
    assert compare_snapshots(current, snapshot('03-31-2026'))[0]
    assert not compare_snapshots(current, snapshot('12-31-2025'))[0]


def test_combination_report_does_not_claim_complete_manager_changes():
    current, previous = snapshot(), snapshot('03-31-2026')
    current['report_type'] = '13F COMBINATION REPORT'
    assert not compare_snapshots(current, previous)[0]
    text = format_snapshot(current, previous)
    assert '不是主体完整持仓' in text and '未进行完整增减持比较' in text


def test_put_call_and_share_class_are_separate_positions():
    rows = [('Issuer', 'A', '123456789', 100, 10, 'SH', ''),
            ('Issuer', 'A', '123456789', 100, 10, 'SH', 'Put'),
            ('Issuer', 'B', '123456789', 100, 10, 'SH', '')]
    result = parse_snapshot(cover(entries=3, total=300), table(rows), '2026-08-14', '1709323')
    assert '共 3 项' in format_snapshot(result)


def test_equivalent_ads_descriptions_do_not_become_false_new_positions():
    current, previous = snapshot(shares=10761119), snapshot('03-31-2026', shares=4608000)
    current['rows'][0]['class'] = 'SPON ADS'
    previous['rows'][0]['class'] = 'SPONSORED ADS'
    change = next(iter(compare_snapshots(current, previous)[0].values()))
    assert change == {'change': '数量增加', 'quantity_delta': 6153119}
    assert '首次披露' not in format_snapshot(current, previous)


def test_unrecognized_class_description_change_is_not_a_new_position():
    current, previous = snapshot(), snapshot('03-31-2026')
    current['rows'][0]['class'] = 'UNKNOWN CLASS'
    change = next(iter(compare_snapshots(current, previous)[0].values()))
    assert change['quantity_delta'] is None and '未核实' in change['change']
    assert '首次披露' not in format_snapshot(current, previous)


def test_small_increased_positions_survive_the_top_holdings_display_limit():
    from copy import deepcopy
    current, previous = snapshot(), snapshot('03-31-2026')
    for index in range(1, 6):
        row = deepcopy(current['rows'][0])
        row.update(issuer=f'Small issuer {index}', cusip=f'{index:09d}', value_usd=index,
                   shares_or_principal=index)
        current['rows'].append(row)
    text = format_snapshot(current, previous, limit=1)
    assert '前 1 项，共 6 项' in text
    assert 'Small issuer 5' in text and 'Small issuer 3' in text
    assert 'Small issuer 2' not in text and 'Small issuer 1' not in text
    assert '最多 3 项，按本期申报市值排序' in text


def test_missing_positions_are_explicit_snapshot_evidence_with_prior_quantities():
    current, previous = snapshot(), snapshot('03-31-2026', shares=7)
    current['rows'][0]['cusip'] = '987654321'
    previous['rows'][0]['issuer'] = 'Prior issuer'
    changes, _ = compare_snapshots(current, previous)
    removed = next(change for change in changes.values() if change['quantity_delta'] < 0)
    assert removed == {'change': '本期表内未再披露该类别', 'quantity_delta': -7}
    text = format_snapshot(current, previous)
    assert 'Prior issuer' in text and '上期数量 7 SH' in text
    assert '不证明实际卖出或清仓' in text


@pytest.mark.parametrize('incomplete', ['current', 'previous'])
def test_incomplete_or_nonadjacent_snapshots_cannot_produce_missing_position_signals(incomplete):
    current, previous = snapshot(), snapshot('03-31-2026')
    current['rows'] = []
    current['total_value_usd'] = current['declared_total_usd'] = 0
    (current if incomplete == 'current' else previous)['complete'] = False
    assert '本期表内未再披露线索' not in format_snapshot(current, previous)
    current['complete'] = previous['complete'] = True
    previous['period'] = '2025-12-31'
    assert '本期表内未再披露线索' not in format_snapshot(current, previous)


def test_valid_empty_current_snapshot_can_show_prior_positions_without_dividing_by_zero():
    current, previous = snapshot(), snapshot('03-31-2026')
    current['rows'] = []
    current['total_value_usd'] = current['declared_total_usd'] = 0
    text = format_snapshot(current, previous)
    assert '前 0 项，共 0 项' in text and '上期数量 10 SH' in text


def test_unknown_class_description_change_is_not_also_reported_as_a_missing_position():
    current, previous = snapshot(), snapshot('03-31-2026')
    current['rows'][0]['class'] = 'UNKNOWN CLASS'
    assert len(compare_snapshots(current, previous)[0]) == 1
    assert '本期表内未再披露线索' not in format_snapshot(current, previous)


@pytest.mark.parametrize('extension', ['xml', 'XML'])
def test_safe_accession_directory_loading_and_raw_xml_urls(extension):
    base = 'https://www.sec.gov/Archives/edgar/data/1709323/000204358526000022/'
    payload = {'directory': {'item': [{'name': 'primary_doc.xml', 'size': '2044'},
                                    {'name': f'table.{extension}', 'size': '4454'},
                                    {'name': '../private.xml'}, {'name': 'https://evil.example/x.xml'}]}}
    responses = {base + 'index.json': Mock(json=Mock(return_value=payload)),
                 base + 'primary_doc.xml': Mock(content=cover()), base + f'table.{extension}': Mock(content=table())}
    get = Mock(side_effect=lambda url: responses[url])
    result = load_snapshot(get, base + 'xslForm13F_X02/primary_doc.xml', '2026-08-14', '1709323')
    assert result['source_url'] == base + f'table.{extension}' and get.call_count == 3
    assert all(call.args[0].startswith(base) for call in get.call_args_list)


def test_loader_skips_unrelated_xml_and_declared_oversized_files_before_valid_table():
    base = 'https://www.sec.gov/Archives/edgar/data/1709323/000204358526000022/'
    listing = [None, {'name': 'primary_doc.xml'}, {'name': 'huge.xml', 'size': '5000001'},
               {'name': 'bad-size.xml', 'size': 'unknown'}, {'name': 'schema.xml'}, {'name': 'table.xml'}]
    responses = {base + 'index.json': Mock(json=Mock(return_value={'directory': {'item': listing}})),
                 base + 'primary_doc.xml': Mock(content=cover()),
                 base + 'schema.xml': Mock(content=b'<schema/>'), base + 'table.xml': Mock(content=table())}
    get = Mock(side_effect=lambda url: responses[url])
    assert load_snapshot(get, base + 'primary_doc.xml', '2026-08-14', '1709323')['source_url'] == base + 'table.xml'
    assert get.call_count == 4
    assert not any('huge.xml' in call.args[0] or 'bad-size.xml' in call.args[0] for call in get.call_args_list)


@pytest.mark.parametrize('listing,message', [(None, 'Invalid accession'),
                                           ([], 'Cover XML is absent'),
                                           ([{'name': 'primary_doc.xml'}], 'No supported information')])
def test_missing_or_invalid_accession_listing_fails_without_unbounded_requests(listing, message):
    base = 'https://www.sec.gov/Archives/edgar/data/1709323/000204358526000022/'
    responses = {base + 'index.json': Mock(json=Mock(return_value={'directory': {'item': listing}})),
                 base + 'primary_doc.xml': Mock(content=cover())}
    get = Mock(side_effect=lambda url: responses[url])
    with pytest.raises(ValueError, match=message):
        load_snapshot(get, base + 'primary_doc.xml', '2026-08-14', '1709323')
    assert get.call_count <= 2


@pytest.mark.parametrize('url', ['https://evil.example/primary_doc.xml',
                               'https://www.sec.gov/Archives/edgar/data/1/000204358526000022/primary_doc.xml',
                               'https://www.sec.gov/Archives/edgar/data/1709323/000204358526000022/../primary_doc.xml'])
def test_loader_rejects_unverified_urls_before_network(url):
    get = Mock()
    with pytest.raises(ValueError):
        load_snapshot(get, url, '2026-08-14', '1709323')
    get.assert_not_called()


def test_enrichment_retains_prior_source_as_historical_comparison_evidence(monkeypatch):
    cfg = Config()
    cfg.SEC_INCLUDE_HOLDINGS = True
    searcher = SecEdgarSearcher(cfg)
    current, previous = snapshot(), snapshot('03-31-2026')
    current['source_url'] = 'https://www.sec.gov/Archives/current-table.xml'
    previous['source_url'] = 'https://www.sec.gov/Archives/prior-table.xml'
    previous['filing_date'] = '2026-05-15'
    records = [{'_filing_type': '13F-HR', '_report_period': '2026-06-30',
                'date': '2026-08-14', 'href': 'https://www.sec.gov/current.xml', 'body': 'metadata'}]
    history = records + [{'_filing_type': '13F-HR', '_report_period': '2026-03-31',
                          'date': '2026-05-15', 'href': 'https://www.sec.gov/prior.xml'}]
    loader = Mock(side_effect=[current, previous])
    monkeypatch.setattr('core.sec_holdings.load_snapshot', loader)
    searcher._enrich_holdings(records, history, '1709323')
    assert loader.call_count == 2 and len(records) == 2
    assert records[1]['href'] == previous['source_url'] and records[1]['_baseline'] is True
    assert '仅作为本期数量比较基线' in records[1]['body']
    assert '上期报告期 2026-03-31' in records[0]['body']
    records[0]['title'] = 'Current filing'
    text = SearchAggregator(SearchService(cfg))._format_results(records, True)
    assert '链接: ' + current['source_url'] in text
    assert '链接: ' + previous['source_url'] in text
    assert '历史比较基线，不计入已核实时效资料条数' in text


def test_prior_failure_keeps_current_holdings_without_inventing_changes(monkeypatch):
    cfg = Config()
    searcher = SecEdgarSearcher(cfg)
    current = snapshot()
    current['source_url'] = 'https://www.sec.gov/Archives/current.xml'
    records = [{'_filing_type': '13F-HR', '_report_period': '2026-06-30',
                'date': '2026-08-14', 'href': 'https://www.sec.gov/current.xml', 'body': 'metadata'}]
    history = records + [{'_filing_type': '13F-HR', '_report_period': '2026-03-31',
                          'date': '2026-05-15', 'href': 'https://www.sec.gov/prior.xml'}]
    monkeypatch.setattr('core.sec_holdings.load_snapshot', Mock(side_effect=[current, OSError('offline')]))
    searcher._enrich_holdings(records, history, '1709323')
    assert '持仓信息表' in records[0]['body']
    assert '未取得上期可比信息表' in records[0]['body']
    assert '数量差' not in records[0]['body'] and len(records) == 1


def test_current_failure_keeps_metadata_without_losing_filing(monkeypatch):
    records = [{'_filing_type': '13F-HR', '_report_period': '2026-06-30',
                'date': '2026-08-14', 'href': 'https://www.sec.gov/current.xml', 'body': 'metadata'}]
    monkeypatch.setattr('core.sec_holdings.load_snapshot', Mock(side_effect=ValueError('invalid table')))
    SecEdgarSearcher(Config())._enrich_holdings(records, records, '1709323')
    assert records[0]['body'] == 'metadata' and len(records) == 1


@pytest.mark.parametrize('name,cik', [('Coatue Management', '0001135730'),
                                   ('Viking Global Investors', '0001103804'),
                                   ('Lone Pine Capital', '0001061165'),
                                   ('Dragoneer Investment Group', '0001602189'),
                                   ('HHLR Advisors', '0001762304')])
def test_new_cik_mappings_resolve_verified_filers(name, cik):
    from config.fund_mappings import get_sec_cik
    assert get_sec_cik(name) == cik
    assert get_sec_cik(name.lower() + '（备注）') == cik


def test_holdings_opt_in_reserves_a_slot_before_recent_13g_metadata(monkeypatch):
    from datetime import date, timedelta
    cfg = Config()
    cfg.SEC_INCLUDE_HOLDINGS = True
    cfg.SEC_USER_AGENT = 'Test test@example.com'
    today = date.today()
    data = {'cik': '1709323', 'name': 'Test Manager', 'filings': {'recent': {
        'form': ['SCHEDULE 13G'] * 3 + ['13F-HR'],
        'filingDate': [today.isoformat()] * 3 + [(today - timedelta(days=20)).isoformat()],
        'accessionNumber': [f'0002043585-26-00000{i}' for i in range(4)],
        'primaryDocument': ['primary_doc.xml'] * 4,
        'reportDate': [''] * 3 + ['2026-06-30'],
    }}}
    searcher = SecEdgarSearcher(cfg)
    searcher._get = Mock(return_value=Mock(json=Mock(return_value=data)))
    searcher._enrich_holdings = Mock()
    results = searcher.search_recent_filings('Himalaya Capital', max_results=3)
    assert results[0]['_filing_type'] == '13F-HR' and len(results) == 3
    assert searcher._enrich_holdings.call_args.args[0][0] is results[0]
    cfg.SEC_INCLUDE_HOLDINGS = False
    assert all(item['_filing_type'] == 'SCHEDULE 13G' for item in
               searcher.search_recent_filings('Himalaya Capital', max_results=3))
