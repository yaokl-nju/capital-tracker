"""Read-only live search probe; runs without LLM calls or email sending."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import Config
from core.search_service import SearchService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query', default='institutional investment financing capital flows')
    parser.add_argument('--market', choices=('global', 'china'), default='global')
    args = parser.parse_args()
    config = Config()
    if args.market == 'china':
        config.SEARCH_REGION, config.SEARCH_GL, config.SEARCH_HL = 'cn-zh', 'CN', 'zh-Hans'
    service = SearchService(config)
    results = service.search(args.query, max_results=3, timelimit='w')
    if results is None:
        print('FAIL: all configured search backends unavailable')
        return 1
    print(f'OK: {len(results)} results; locale={config.SEARCH_GL}')
    for result in results:
        print(f"  {result['_domain']} score={result['_score']} dated={bool(result['date'])}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
