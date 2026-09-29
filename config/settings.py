"""Configuration settings for the intelligence gathering system."""
import os
from openai import OpenAI


class LLMConfig:
    """LLM backend configuration."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        name: str = "default"
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.name = name

    def create_client(self) -> OpenAI:
        """Create an OpenAI client with this configuration."""
        return OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=60, max_retries=0)


class Config:
	"""Global configuration for the tracking system."""

	# LLM configurations
	DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
	DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
	DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

	# Search backends (see core/search_service.py -> SearchService)
	#
	# Backends are tried in SEARCH_BACKENDS order until one returns results.
	# Supported backend names (each maps to a `_try_<name>` method in SearchService):
	#   ddgs_news / ddgs_text - DDGS news / text (free, no key)
	#   ddgs - legacy alias for ddgs_text
	#   serper - Google SERP via serper.dev (needs SERPER_API_KEY)
	#   tavily - agent-native search API (needs TAVILY_API_KEY)
	#   brave  - Brave Search API (needs BRAVE_API_KEY)
	#   parallel - Parallel Search API (needs PARALLEL_SEARCH_API_KEY)
	#   gdelt - optional keyless news index (explicit opt-in; service limits apply)
	# A backend whose API key is empty is skipped automatically.
	SEARCH_BACKENDS = ["ddgs_news", "brave", "parallel", "serper", "tavily", "ddgs_text"]

	# Result locale / language.
	# Chinese presets:
	#   SEARCH_REGION = "cn-zh"     (DuckDuckGo region code)
	#   SEARCH_GL     = "CN"        (Serper/Brave country code)
	#   SEARCH_HL     = "zh-Hans"   (Serper result language)
	# Worldwide-English defaults are shown below.
	SEARCH_REGION = "wt-wt"
	SEARCH_GL = "US"
	SEARCH_HL = "en"
	SEARCH_SAFESEARCH = "moderate"
	SEARCH_TIMEOUT = 15  # seconds per backend request
	# Conservative request spacing for concurrent workers; adjust to your plan.
	SEARCH_MIN_INTERVALS = {"brave": 1.0}

	# Optional shared proxy for backends that are unreachable from the local
	# network (e.g. mainland China reaching Brave/DDG/Google). Empty = no proxy.
	# Used by ddgs and brave backends (and any requests-based backend via proxy map).
	SEARCH_PROXY = os.getenv("SEARCH_PROXY", "")

	# ---- Provider API keys (empty string disables that backend) ----
	BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
	PARALLEL_SEARCH_API_KEY = os.getenv("PARALLEL_SEARCH_API_KEY", "")
	SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")
	TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

	# Processing limits
	MAX_TRIALS = 1
	MAX_RESULTS = 5
	CONCURRENCY = 4
	NEWS_TIMELIMIT = 'w'  # 'd'=day, 'w'=week

	MAX_QUERIES = 12
	MAX_RAW_DATA_CHARS = 40000
	SEC_LOOKBACK_DAYS = 120
	SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "")
	SEC_INCLUDE_HOLDINGS = False  # opt in with --sec-holdings; extra official XML requests
	SEC_MAX_POSITIONS = 10
	SEARCH_DELAY_RANGE = (1, 1)
	MIN_ANALYSIS_ITEMS = 5
	# Separate process-wide gates cap general search and SEC at 10 starts/s and 10 in flight each.
	SEARCH_CONCURRENCY = 10

	# Search source filtering (domain allowlist/denylist)
	# Allowlist: only keep results from these domains (empty = no filter)
	SOURCE_ALLOWLIST = set()
	# Denylist: block results from these domains
	SOURCE_DENYLIST = set([
		'ads.', 'ad.', 'tracking.', 'analytics.', 'stats.', 'pixel.',
	])

	# Search cache (memory cache for repeated queries)
	ENABLE_SEARCH_CACHE = True
	SEARCH_CACHE_TTL = 3600  # seconds (1 hour)
	SEARCH_CACHE_PATH = os.getenv('SEARCH_CACHE_PATH', '')  # optional SQLite cache

	# Pre-configured LLM backends
	DEEPSEEK = LLMConfig(
		api_key=DEEPSEEK_API_KEY,
		base_url=DEEPSEEK_BASE_URL,
		model=DEEPSEEK_MODEL,
		name="deepseek"
	)

	# Optional email configuration
	DEFAULT_EMAIL = os.getenv("EMAIL_SENDER", "")
	DEFAULT_EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
	EMAIL_SMTP_SERVER = "smtp.qq.com"
	EMAIL_SMTP_PORT = 465
