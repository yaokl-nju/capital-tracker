"""Main entry point for the advanced intelligence gathering system."""
import os
import argparse
import json
from copy import copy
from typing import Optional

from trackers.investment import InvestmentTracker
from core.orchestrator import Orchestrator
from core.email_service import EmailConfig
from config.settings import Config
from core.search_service import SearchService, SEARCH_BACKEND_NAMES


# Reports stay within the project unless an output directory is supplied.
OUTPUT_DIRS = {
    'investment': os.path.join(os.path.dirname(__file__), 'reports', 'international'),
    'investment_china': os.path.join(os.path.dirname(__file__), 'reports', 'china'),
}

# Default topics for each tracker


DEFAULT_TOPICS = {
    'investment': [
        # =======================================================
        # 第一梯队：全球顶级基本面成长 / Stock Picker
        # 最值得跟踪“新建仓、明显加仓、退出”
        # =======================================================
        "Coatue Management",
        "Viking Global Investors",
        "Lone Pine Capital",
        "D1 Capital Partners",
        "Tiger Global",
        "Whale Rock Capital",          # 科技/AI/软件，非常有价值
        "Dragoneer Investment Group",  # 公募+成长股+Pre-IPO
        "Baillie Gifford",             # 超长期成长

        # # =======================================================
        # # 第二梯队：顶级集中型 / 宏观+基本面投资人
        # # 仓位变化通常信息量很大
        # # =======================================================
        # "Duquesne Family Office",
        # "Appaloosa Management",
        # "Pershing Square",
        # "Third Point",
        # "TCI Fund Management",
        # "Himalaya Capital",
        # "Berkshire Hathaway",          
        # "Baupost Group",               # 价值/特殊机会
        # "Elliott Investment Management", # 激进投资/事件驱动

        # # =======================================================
        # # 第三梯队：全球主动型长线机构
        # # 不适合简单抄作业，更适合确认产业大趋势
        # # =======================================================
        # "GQG Partners",
        # "Capital Group",
        # "T. Rowe Price",
        # "Fidelity",
        # "Wellington Management",

        # # =======================================================
        # # 第四梯队：医疗 / Biotech 专业资本
        # # 医药股建议单独分析
        # # =======================================================
        # "Baker Bros. Advisors",
        # "RA Capital Management",
        # "Perceptive Advisors",
        # "EcoR1 Capital",
        # "OrbiMed",                     

        # # =======================================================
        # # 第五梯队：宏观 / 资产配置
        # # 看方向，不宜直接用个股持仓做抄作业
        # # =======================================================
        # "Bridgewater Associates",
        # "Soros Fund Management",

        # # =======================================================
        # # 第六梯队：量化 / 多经理平台
        # # 主要观察资金流，不作为基本面选股信号
        # # =======================================================
        # "Two Sigma",
    ],
    'investment_china': [
        # =======================================================
        # 第一梯队：中国顶级二级市场 Stock Picker
        # 单只股票的建仓/加仓信息含量最高
        # =======================================================
        "Perseverance Asset Management",  # 高毅：冯柳/邓晓峰等
        "Greenwoods Asset Management",    # 景林
        "Springs Capital",                # 淡水泉

        # # =======================================================
        # # 第二梯队：全球资金中的中国核心主动投资者
        # # 港股/中概股/海外上市中国公司尤其重要
        # # =======================================================
        # "HHLR Advisors",                  # Hillhouse / 高瓴
        # "Schroders",

        # # =======================================================
        # # 第三梯队：全球大型机构 / 配置型资本
        # # 看外资整体态度，不把单笔交易当成强选股信号
        # # =======================================================
        # "BlackRock",
        # "JPMorgan Asset Management",

        # # =======================================================
        # # 第四梯队：中国一级市场 / Pre-IPO / 产业趋势资本
        # # 判断未来3-10年的产业方向
        # # =======================================================
        # "HSG",                            # HongShan / 红杉中国
        # "Loyal Valley Capital",
        # "Boyu Capital",                   
        # "Primavera Capital",              # 春华资本，建议新增
        # "FountainVest Partners",          # 方源资本，建议新增
        # "CPE",                            # 中信产业基金体系
        # "General Atlantic",               # 全球成长资本
        # "Warburg Pincus",                 # 中国投资历史很长

        # # =======================================================
        # # 第五梯队：中国公募：本土机构确认信号
        # # =======================================================
        # "E Fund",
        # "Fullgoal",

        # # =======================================================
        # # 第六梯队：主权财富基金
        # # =======================================================
        # "Temasek",
        # "GIC",
        # "Norges Bank Investment Management",  # 挪威主权基金
        # "CPP Investments",
        # "Mubadala",
    ],
}

REPORT_TITLES = {
    'investment': '国际金融资本动态简报',
    'investment_china': '国内金融资本动态简报',
}
FILENAME_PREFIXES = {'investment': 'Capital_Global', 'investment_china': 'Capital_China'}
TRACKER_CLASSES = {'investment': InvestmentTracker, 'investment_china': InvestmentTracker}


def run_tracker(
    tracker_type: str,
    topics: Optional[list[str]] = None,
    output_dir: Optional[str] = None,
    max_workers: Optional[int] = None,
    send_email: bool = False,
    email_addr: Optional[str] = None,
    email_password: Optional[str] = None,
    config: Optional[Config] = None
) -> str:
    """
    Run a specific tracker.

    Args:
        tracker_type: Type of tracker ('investment', 'investment_china')
        topics: List of topics to track (default from tracker defaults)
        output_dir: Output directory for PDF
        max_workers: Max concurrent workers
        send_email: Whether to send PDF via email
        email_addr: Email address for sending
        email_password: Email password for sending

    Returns:
        Path to generated PDF file
    """
    # Get tracker class and default config
    tracker_class = TRACKER_CLASSES.get(tracker_type)
    if not tracker_class:
        raise ValueError(f"Unknown tracker type: {tracker_type}")

    topics = DEFAULT_TOPICS[tracker_type] if topics is None else topics
    if not topics:
        raise ValueError("At least one topic is required")
    output_dir = output_dir or OUTPUT_DIRS[tracker_type]

    # Configure email if needed
    email_config = None
    if send_email:
        # Use Config defaults if not provided
        sender_email = Config.DEFAULT_EMAIL or email_addr
        sender_password = email_password or Config.DEFAULT_EMAIL_PASSWORD
        if not sender_email or not sender_password:
            raise ValueError("Email requires EMAIL_SENDER and EMAIL_PASSWORD (or CLI credentials)")
        email_config = EmailConfig(
            sender_email=sender_email,
            sender_password=sender_password,
            smtp_server=Config.EMAIL_SMTP_SERVER,
            smtp_port=Config.EMAIL_SMTP_PORT
        )

    options = {'market': 'china' if tracker_type == 'investment_china' else 'global'}
    if config is not None:
        options['config'] = config
    tracker = tracker_class(**options)
    orchestrator = Orchestrator(tracker)

    # Run pipeline
    return orchestrator.run(
        topics=topics,
        output_dir=output_dir,
        filename_prefix=FILENAME_PREFIXES[tracker_type],
        report_title=REPORT_TITLES[tracker_type],
        max_workers=max_workers,
        send_email=send_email,
        email_config=email_config,
        recipient_email=email_addr or Config.DEFAULT_EMAIL
    )


def main():
    """Main entry point with CLI interface."""
    parser = argparse.ArgumentParser(
        description='金融资本资讯助手'
    )
    parser.add_argument(
        '--tracker',
        choices=['investment', 'investment_china', 'all'],
        default='all',
        help='Type of tracker to run'
    )
    parser.add_argument(
        '--topics',
        nargs='+',
        help='List of topics to track (default: tracker-specific defaults)'
    )
    parser.add_argument(
        '--output-dir',
        help='Output directory for PDF reports'
    )
    parser.add_argument(
        '--max-workers',
        type=int,
        help='Maximum concurrent workers'
    )
    parser.add_argument(
        '--email',
        help='Email address to send report to'
    )
    parser.add_argument(
        '--email-password',
        help='Email password/authorization code'
    )
    parser.add_argument('--doctor', action='store_true', help='Show local readiness without network calls or key values')
    parser.add_argument('--max-queries', type=int, help='Maximum queries per topic (1–50)')
    parser.add_argument('--max-results', type=int, help='Maximum results per query (1–20)')
    parser.add_argument('--timelimit', choices=['d', 'w', 'm', 'y', 'none'], help='News date window')
    parser.add_argument('--search-backends', nargs='+', choices=SEARCH_BACKEND_NAMES,
                        help='Override search priority; gdelt is an optional free news index')
    parser.add_argument('--no-cache', action='store_true', help='Bypass all search caches')
    parser.add_argument('--cache-path', help='Optional SQLite search cache shared between runs')
    parser.add_argument('--sec-holdings', action='store_true', help='Read verified SEC 13F XML holdings and a prior snapshot')

    args = parser.parse_args()

    if args.max_workers is not None and args.max_workers < 1:
        parser.error('--max-workers must be positive')
    for name, upper in [('max_queries', 50), ('max_results', 20)]:
        value = getattr(args, name)
        if value is not None and not 1 <= value <= upper:
            parser.error(f'--{name.replace("_", "-")} must be between 1 and {upper}')
    config = Config()
    if args.max_queries is not None:
        config.MAX_QUERIES = args.max_queries
    if args.max_results is not None:
        config.MAX_RESULTS = args.max_results
    if args.timelimit is not None:
        config.NEWS_TIMELIMIT = None if args.timelimit == 'none' else args.timelimit
    if args.search_backends:
        config.SEARCH_BACKENDS = list(dict.fromkeys(args.search_backends))
    if args.no_cache:
        config.ENABLE_SEARCH_CACHE = False
    if args.cache_path:
        config.SEARCH_CACHE_PATH = args.cache_path
    if args.sec_holdings:
        config.SEC_INCLUDE_HOLDINGS = True
    if args.doctor:
        show_readiness(config)
        return

    if args.tracker == 'all':
        failed = False
        # Run both capital markets
        for tracker_type in TRACKER_CLASSES.keys():
            print(f"\n{'='*60}")
            print(f"Running {tracker_type.upper()} tracker...")
            print(f"{'='*60}\n")
            try:
                run_tracker(
                    tracker_type=tracker_type,
                    topics=args.topics,
                    output_dir=args.output_dir,
                    max_workers=args.max_workers,
                    send_email=bool(args.email),
                    email_addr=args.email,
                    email_password=args.email_password,
                    config=config
                )
            except Exception as e:
                failed = True
                print(f"❌ {tracker_type} tracker failed: {e}")
        if failed:
            raise SystemExit(1)
    else:
        # Run single tracker
        run_tracker(
            tracker_type=args.tracker,
            topics=args.topics,
            output_dir=args.output_dir,
            max_workers=args.max_workers,
            send_email=bool(args.email),
            email_addr=args.email,
            email_password=args.email_password,
            config=config
        )


def show_readiness(config):
    """Print presence/readiness only; this does not test remote credentials."""
    from core import search_service
    try:
        import weasyprint  # noqa: F401
        pdf_ready = True
    except (ImportError, OSError):
        pdf_ready = False
    probe_config = copy(config)
    probe_config.ENABLE_SEARCH_CACHE = False
    search = SearchService(probe_config)
    enabled = search._enabled_backends()
    if search_service.DDGS is None:
        enabled = [name for name in enabled if not name.startswith('ddgs')]
    print(json.dumps({
        'network_checked': False,
        'model': config.DEEPSEEK.model,
        'model_key_present': bool(config.DEEPSEEK.api_key),
        'search_priority': config.SEARCH_BACKENDS,
        'locally_available_backends': enabled,
        'sec_user_agent_present': bool(config.SEC_USER_AGENT),
        'sec_holdings_enabled': config.SEC_INCLUDE_HOLDINGS,
        'pdf_runtime_ready': pdf_ready,
        'cache_enabled': config.ENABLE_SEARCH_CACHE,
        'disk_cache_configured': bool(config.SEARCH_CACHE_PATH),
        'max_queries': config.MAX_QUERIES,
        'max_results': config.MAX_RESULTS,
        'news_timelimit': config.NEWS_TIMELIMIT,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
