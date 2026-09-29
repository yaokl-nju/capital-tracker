"""Fund name mappings for SEC EDGAR API search.

Maps human-readable fund names to their official SEC EDGAR company names.
This improves search success rates by using exact SEC-registered names.
"""

# Fund name mapping: display_name → SEC EDGAR company name
FUND_MAPPINGS = {
    # Value-oriented investment funds
    "Himalaya Capital": "Himalaya Capital Management LLC",
    "Pershing Square": "Pershing Square Capital Management, L.P.",
    "Baillie Gifford (柏基投资)": "Baillie Gifford & Co",
    "Oaktree Capital": "Oaktree Capital Management, L.P.",
    "Duquesne Family Office": "Duquesne Family Office, LLC",
    "Bridgewater Associates (桥水)": "BRIDGEWATER ASSOCIATES, LP",
    "Bridgewater Associates": "BRIDGEWATER ASSOCIATES, LP",

    # Hedge funds
    "Citadel": "Citadel Advisors LLC",
    "Tiger Global (老虎环球)": "Tiger Global Management, LLC",
    "Tiger Global": "Tiger Global Management, LLC",

    # Sovereign wealth funds
    "GIC (新加坡政府投资公司)": "GIC PRIVATE LIMITED",
    "GIC": "GIC PRIVATE LIMITED",
    "Temasek (淡马锡)": "TEMASEK HOLDINGS (PRIVATE) LIMITED",
    "Temasek": "TEMASEK HOLDINGS (PRIVATE) LIMITED",

    # Venture capital
    "Sequoia Capital (红杉资本)": "Sequoia Capital Operations, LLC",
    "Sequoia Capital": "Sequoia Capital Operations, LLC",
    "Founders Fund": "Founders Fund Investment Management, LLC",
    "Lightspeed Capital": "Lightspeed Venture Partners",
    "NEA": "New Enterprise Associates",
    "Accel": "Accel Partners",
    "Benchmark Capital": "Benchmark Capital",

    # Corporate investment arms
    "SoftBank Vision Fund": "SoftBank Group International LLC",
    "SoftBank": "SoftBank Corp.",
    "Blackstone Capital": "Blackstone Inc.",
    "Blackstone": "Blackstone Inc.",
    "Silver Lake Capital": "Silver Lake Partners",
    "Insight Partners": "Insight Partners Fund IV, L.P.",
}


def get_sec_edgar_name(fund_name: str) -> str:
    """
    Get the SEC EDGAR company name for a given fund name.

    Args:
        fund_name: The display name of the fund

    Returns:
        The SEC EDGAR company name, or the original name if not found
    """
    # Try exact match first
    fund_name = fund_name.strip()
    names = {name.casefold(): registered for name, registered in FUND_MAPPINGS.items()}
    if fund_name.casefold() in names:
        return names[fund_name.casefold()]

    # Try partial match (handle cases with Chinese notes in parentheses)
    # Extract base name before parentheses or Chinese characters
    import re
    base_name = re.sub(r'\s*[\(（].*?[\)）]\s*', '', fund_name).strip()

    if base_name.casefold() in names:
        return names[base_name.casefold()]

    # Return original name if no mapping found
    return fund_name


def get_all_fund_names() -> list[str]:
    """Get display names recognized by the mapping."""
    return list(FUND_MAPPINGS.keys())


# Search aliases are not SEC legal-entity mappings. Preserve that distinction.
SEARCH_ALIASES = {
    'Perseverance Asset Management': '高毅资产',
    'Greenwoods Asset Management': '景林资产',
    'Springs Capital': '淡水泉',
    'HHLR Advisors': '高瓴 HHLR',
    'HSG': '红杉中国 HongShan',
    'Loyal Valley Capital': '正心谷资本',
    'Boyu Capital': '博裕资本',
    'Primavera Capital': '春华资本',
    'FountainVest Partners': '方源资本',
    'CPE': '中信产业基金 CPE源峰',
    'E Fund': '易方达基金',
    'Fullgoal': '富国基金',
    'BlackRock': '贝莱德',
    'Schroders': '施罗德',
    'JPMorgan Asset Management': '摩根资产管理',
    'General Atlantic': '泛大西洋投资',
    'Warburg Pincus': '华平投资',
    'Temasek': '淡马锡',
    'GIC': '新加坡政府投资公司 GIC',
}


def get_search_names(topic: str) -> list[str]:
    """Return display name and known bilingual alias for financial search."""
    import re
    name = re.sub(r'\s*[（(].*?[）)]', '', topic).strip()
    aliases = {english.casefold(): (english, chinese) for english, chinese in SEARCH_ALIASES.items()}
    if name.casefold() in aliases:
        return list(aliases[name.casefold()])
    for english, chinese in SEARCH_ALIASES.items():
        if name == chinese or name in chinese.split():
            return [english, chinese]
    return [name]


# Verified filer identifiers. Keep uncertain legal entities out of this table.
# https://www.sec.gov/Archives/edgar/data/1709323/000204358525000004/0002043585-25-000004-index.html
# https://www.sec.gov/Archives/edgar/data/1393818/0000950123-25-005761-index.htm
FUND_CIKS = {
    'Himalaya Capital': '0001709323',
    'Himalaya Capital Management LLC': '0001709323',
    'Blackstone': '0001393818',
    'Blackstone Capital': '0001393818',
    'Blackstone Inc.': '0001393818',
    # Each identifier is verified against the SEC filing index, not inferred from a brand.
    # https://www.sec.gov/Archives/edgar/data/1135730/000091957426005478/0000919574-26-005478-index.htm
    'Coatue Management': '0001135730',
    'Coatue Management LLC': '0001135730',
    # https://www.sec.gov/Archives/edgar/data/1103804/000110380426000004/0001103804-26-000004-index.html
    'Viking Global Investors': '0001103804',
    'Viking Global Investors LP': '0001103804',
    # https://www.sec.gov/Archives/edgar/data/1061165/000091957426005485/0000919574-26-005485-index.html
    'Lone Pine Capital': '0001061165',
    'Lone Pine Capital LLC': '0001061165',
    # https://www.sec.gov/Archives/edgar/data/1602189/000119312526225175/0001193125-26-225175-index.htm
    'Dragoneer Investment Group': '0001602189',
    'Dragoneer Investment Group, LLC': '0001602189',
    # https://www.sec.gov/Archives/edgar/data/1762304/000091957426005568/0000919574-26-005568-index.htm
    'HHLR Advisors': '0001762304',
    'HHLR Advisors, Ltd.': '0001762304',
}


def get_sec_cik(fund_name: str):
    """Resolve verified names or explicit CIKs without guessing an entity."""
    import re
    name = re.sub(r'\s*[（(].*?[）)]', '', fund_name).strip()
    if re.fullmatch(r'\d{1,10}', name) and int(name):
        return f'{int(name):010d}'
    registered = get_sec_edgar_name(name)
    for key, cik in FUND_CIKS.items():
        if key.casefold() in (name.casefold(), registered.casefold()):
            return cik
    return None
