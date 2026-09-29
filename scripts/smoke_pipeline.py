"""Bounded live check of query generation, capital search, analysis and PDF."""
import argparse
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import Config
from core.orchestrator import Orchestrator
from trackers.investment import InvestmentTracker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--topic', default='BlackRock')
    parser.add_argument('--market', choices=('global', 'china'), default='global')
    parser.add_argument('--output-dir', default='reports/validation')
    args = parser.parse_args()
    config = Config()
    config.MAX_QUERIES = 2
    config.MAX_RESULTS = 3
    config.MAX_TRIALS = 1
    tracker = InvestmentTracker(config, market=args.market)
    started = time.monotonic()
    orchestrator = Orchestrator(tracker)
    output = orchestrator.run(
        [args.topic], args.output_dir, 'Capital_Smoke_' + args.market,
        '金融资本链路验证', max_workers=1)
    print('Pipeline seconds:', round(time.monotonic() - started, 2))
    if not Path(output).is_file():
        return 1
    print('Report created:', output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
