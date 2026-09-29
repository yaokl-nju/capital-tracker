"""Visible institution-name matching, keeping generic words out of brand anchors."""
import re


SUFFIX_WORDS = {'capital', 'management', 'asset', 'assets', 'investment', 'investments',
                'investor', 'investors', 'group', 'fund', 'partners', 'holdings',
                'inc', 'llc', 'lp', 'ltd', 'advisors', 'advisers', 'the'}
# Observed collisions: mountains/IPO, Mars rover/virtue, seasons/real-estate projects.
AMBIGUOUS_SINGLE_BRANDS = {'himalaya', 'perseverance', 'springs'}


def _phrase(words):
    return r'(?<![a-z0-9])' + r'[^a-z0-9\u4e00-\u9fff]*'.join(map(re.escape, words)) + r'(?![a-z0-9])'


def mentions_topic(item, names):
    """Match full names or distinctive brand phrases, not isolated common surnames."""
    patterns = []
    for name in names:
        words = re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+', name.casefold())
        if not words or len(''.join(words)) < 2:
            continue
        if len(words) == 1 and words[0] in SUFFIX_WORDS:
            continue
        patterns.append(_phrase(words))
        english = [word for word in words if not re.search(r'[\u4e00-\u9fff]', word)]
        brand = [word for word in english if word not in SUFFIX_WORDS]
        ambiguous = len(brand) == 1 and brand[0] in AMBIGUOUS_SINGLE_BRANDS
        if brand and not ambiguous and (len(''.join(brand)) >= 3 or (re.search('[a-z]', brand[0]) and re.search('[0-9]', brand[0]))):
            patterns.append(_phrase(brand))
        for word in words:
            if re.search(r'[\u4e00-\u9fff]', word):
                patterns.append(re.escape(word))
                short = re.sub(r'(?:资产管理|投资管理|基金管理|资本管理|基金|资产|资本|投资|控股|集团|管理|公司)$', '', word)
                if len(short) >= 2:
                    patterns.append(re.escape(short))
    if not patterns:
        return True
    fields = [str(item.get(field) or '').casefold() for field in ('title', 'body', 'href')]
    return any(re.search(pattern, text) for pattern in patterns for text in fields)
