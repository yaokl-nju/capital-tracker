"""Base tracker class for intelligence gathering modules."""
from abc import ABC, abstractmethod
from typing import List
import json
import time
import asyncio
from itertools import zip_longest
import re
from threading import RLock
from copy import deepcopy
from datetime import datetime, timezone

from core.llm_service import LLMService
from core.search_service import SearchAggregator, canonical_url
from config.settings import Config


class QueryGenerator(ABC):
    """Abstract base for search query generation."""

    @abstractmethod
    def get_prompt(self, topic: str) -> str:
        """Get the prompt for generating search queries."""
        pass

    @abstractmethod
    def get_fallback_queries(self, topic: str) -> List[str]:
        """Get fallback queries when LLM fails."""
        pass


class Summarizer(ABC):
    """Abstract base for information summarization."""

    @abstractmethod
    def get_prompt(self, topic: str, raw_data: str) -> str:
        """Get the prompt for summarizing information."""
        pass


class Tracker:
    """
    Base tracker class for intelligence gathering.

    Each tracker uses:
    1. A query generator to create search keywords
    2. A search aggregator to collect information
    3. A summarizer to analyze and format results
    """

    def __init__(
        self,
        llm_service: LLMService,
        search_aggregator: SearchAggregator,
        query_generator: QueryGenerator,
        summarizer: Summarizer,
        config: Config = None
    ):
        """
        Initialize tracker.

        Args:
            llm_service: LLM service for API calls
            search_aggregator: Search aggregator for web searches
            query_generator: Query generator instance
            summarizer: Summarizer instance
            config: Configuration object
        """
        self.llm = llm_service
        self.search_aggregator = search_aggregator
        self.query_generator = query_generator
        self.summarizer = summarizer
        self.config = config or Config()
        self.topic_records = {}
        self._records_lock = RLock()

    def snapshot_records(self):
        with self._records_lock:
            return deepcopy(self.topic_records)

    def _record_topic(self, topic, queries, raw_data, summary, started, error_type=''):
        status = ('error' if error_type else 'no_evidence' if not raw_data else
                  'evidence_only' if summary.startswith('### 自动分析不可用') else 'analyzed')
        with self._records_lock:
            self.topic_records[topic] = {
                'completed_at': datetime.now(timezone.utc).isoformat(),
                'duration_seconds': round(time.monotonic() - started, 3),
                'status': status,
                'error_type': error_type,
                'queries': list(queries),
                'raw_evidence': raw_data,
            }

    def generate_queries(self, topic: str) -> List[str]:
        """
        Generate search queries for a topic.

        Args:
            topic: Topic to search for

        Returns:
            List of search query strings
        """
        prompt = self.query_generator.get_prompt(topic)

        for idx in range(self.config.MAX_TRIALS):
            try:
                content = self.llm.complete(
                    prompt,
                    response_format={"type": "json_object"},
                    max_trials=1
                )
                if content is None:
                    break

                content = content.replace("```json", "").replace("```", "").strip()
                content = json.loads(content)

                values = content.get('queries', []) if isinstance(content, dict) else content
                if isinstance(content, dict) and 'queries' not in content:
                    groups = [group for group in content.values() if isinstance(group, list)]
                    values = [q for row in zip_longest(*groups) for q in row if q is not None]
                if not isinstance(values, list):
                    raise ValueError('queries must be a list')
                results = list(dict.fromkeys(q.strip() for q in values
                                            if isinstance(q, str) and q.strip()))
                if not results:
                    raise ValueError('No valid search queries')
                results = results[:self.config.MAX_QUERIES]
                print(f"⚙️  [{topic}] 生成了 {len(results)} 个搜索词")
                return results

            except Exception as e:
                print(f"⚠️ {topic} 第 {idx+1} 次搜索词解析失败: {e}")
                if idx + 1 < self.config.MAX_TRIALS:
                    time.sleep(1)

        # Fall back to default queries
        print(f"⚠️ 未能为 {topic} 成功解析搜索词，使用默认关键词")
        results = self.query_generator.get_fallback_queries(topic)[:self.config.MAX_QUERIES]
        print(f"⚙️  [{topic}] 生成了 {len(results)} 个搜索词")
        return results

    def perform_search(self, topic: str, queries: List[str]) -> str:
        """
        Perform web search and aggregate results.

        Args:
            topic: Topic name for logging
            queries: List of search queries

        Returns:
            Aggregated search results as string
        """
        print(f"🌐 [{topic}] 扫描全网资源...")
        return self.search_aggregator.aggregate(queries, timelimit=self.config.NEWS_TIMELIMIT)

    def summarize(self, topic: str, raw_data: str) -> str:
        """
        Summarize and analyze search results.

        Args:
            topic: Topic being analyzed
            raw_data: Search results to analyze

        Returns:
            Summarized report in markdown format
        """
        if not raw_data:
            return "### 当前检索未找到可核实的近期资本动态"

        print(f"🧠 [{topic}] 深度分析中...")

        prompt = self.summarizer.get_prompt(topic, raw_data)
        result = self.llm.complete(prompt)

        if not result or not result.strip():
            return self._evidence_fallback(raw_data)
        # Models sometimes wrap a source citation in inline code, breaking PDF links.
        result = re.sub(r'`(\[[^\]\n]*\]\(https?://[^\s)]+\))`', r'\1', result)
        allowed = {canonical_url(url) for url in re.findall(r'^链接: (https?://[^\s]+)', raw_data, re.M)}
        cited = re.findall(r'\[[^\]]*\]\(([^\s]+)\)', result)
        invalid = {url for url in cited if not canonical_url(url) or canonical_url(url) not in allowed}
        if invalid:
            print(f'⚠️ [{topic}] 个别来源链接未匹配，标注后保留分析')
            result = re.sub(r'\[([^\]]*)\]\(([^\s]+)\)',
                            lambda match: (match[1] + '（来源链接未核实）'
                                           if match[2] in invalid else match[0]), result)
        return result

    @staticmethod
    def _evidence_fallback(raw_data):
        return ("### 自动分析不可用，保留原始检索证据\n\n"
                "以下为搜索摘要，未完成分析与交叉核实：\n\n" +
                re.sub(r'链接: (https?://[^\s]+)', r'链接: [原始来源](\1)', raw_data))

    def process_topic(self, topic: str) -> tuple[str, str]:
        """
        Process a single topic from query generation to summary.

        Args:
            topic: Topic to process

        Returns:
            Tuple of (topic, summary)
        """
        started = time.monotonic()
        queries, raw_data, summary, error_type = [], '', '', ''
        try:
            queries = self.generate_queries(topic)
            raw_data = self.perform_search(topic, queries)
            summary = self.summarize(topic, raw_data)
            return topic, summary
        except Exception as e:
            error_type = type(e).__name__
            summary = f"数据处理失败: {error_type}"
            return topic, summary
        finally:
            self._record_topic(topic, queries, raw_data, summary, started, error_type)
    async def perform_search_async(self, topic: str, queries: List[str]) -> str:
        return await self.search_aggregator.aggregate_async(
            queries, timelimit=self.config.NEWS_TIMELIMIT)

    async def process_topic_async(self, topic: str) -> tuple[str, str]:
        """Overlap blocking SDK calls without changing their existing implementations."""
        started = time.monotonic()
        queries, raw_data, summary, error_type = [], '', '', ''
        try:
            queries = await asyncio.to_thread(self.generate_queries, topic)
            raw_data = await self.perform_search_async(topic, queries)
            summary = await asyncio.to_thread(self.summarize, topic, raw_data)
            return topic, summary
        except asyncio.CancelledError:
            error_type = 'CancelledError'
            raise
        except Exception as exc:
            error_type = type(exc).__name__
            summary = f"数据处理失败: {error_type}"
            return topic, summary
        finally:
            self._record_topic(topic, queries, raw_data, summary, started, error_type)
