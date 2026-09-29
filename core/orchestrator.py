"""Orchestrator for running multiple trackers."""
import os
import datetime
import asyncio
from typing import List, Optional

from .tracker import Tracker
from .report_generator import ReportGenerator
from .email_service import EmailConfig, EmailService
from config.settings import Config


class Orchestrator:
    """
    Orchestrator for running tracking tasks across multiple topics.

    Manages parallel processing of topics and generates consolidated reports.
    """

    def __init__(
        self,
        tracker: Tracker,
        config: Config = None
    ):
        """
        Initialize orchestrator.

        Args:
            tracker: Tracker instance to use
            config: Configuration object
        """
        self.tracker = tracker
        self.config = config or tracker.config

    def run_topics(
        self,
        topics: List[str],
        max_workers: Optional[int] = None
    ) -> dict[str, str]:
        """
        Process multiple topics in parallel.

        Args:
            topics: List of topics to process
            max_workers: Max concurrent workers (default from config)

        Returns:
            Dictionary mapping topic to summary
        """
        return asyncio.run(self.run_topics_async(topics, max_workers))

    async def run_topics_async(self, topics, max_workers=None):
        max_workers = self.config.CONCURRENCY if max_workers is None else max_workers
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        topics = list(dict.fromkeys(t.strip() for t in topics if isinstance(t, str) and t.strip()))
        if not topics:
            raise ValueError("At least one non-empty topic is required")
        print(f"🚀 开始异步情报收集任务 ({len(topics)} 个主题)...")
        semaphore = asyncio.Semaphore(max_workers)

        async def process(topic):
            async with semaphore:
                try:
                    method = getattr(self.tracker, 'process_topic_async', None)
                    if method is not None:
                        _, summary = await method(topic)
                    else:
                        _, summary = await asyncio.to_thread(self.tracker.process_topic, topic)
                    return topic, summary
                except Exception as exc:
                    return topic, f"处理失败: {exc}"

        return dict(await asyncio.gather(*(process(topic) for topic in topics)))

    def generate_report(
        self,
        summaries: dict[str, str],
        output_dir: str,
        filename_prefix: str,
        report_title: str,
        send_email: bool = False,
        email_config: Optional[EmailConfig] = None,
        recipient_email: Optional[str] = None
    ) -> str:
        """
        Generate PDF report from summaries.

        Args:
            summaries: Dictionary of topic -> summary
            output_dir: Output directory path
            filename_prefix: Prefix for filename
            report_title: Title shown on report cover
            send_email: Whether to send via email
            email_config: Email configuration (if sending)
            recipient_email: Recipient email address (if sending)

        Returns:
            Path to generated PDF file
        """
        if send_email and (not email_config or not recipient_email):
            raise ValueError("Sending email requires sender credentials and recipient")
        # Create output directory if needed
        os.makedirs(output_dir, exist_ok=True)

        # Generate filename
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        filename = f"{filename_prefix}_{timestamp}.pdf"
        output_path = os.path.join(output_dir, filename)

        # Generate PDF
        gen = ReportGenerator(output_path, topic=report_title, config=self.config)
        gen.create_pdf(summaries)

        # Send email if requested
        if send_email and email_config and recipient_email:
            email_service = EmailService(email_config)
            if not email_service.send_with_pdf(output_path, recipient_email):
                raise RuntimeError(f"PDF 已生成，但邮件发送失败: {output_path}")

        return output_path

    def run(
        self,
        topics: List[str],
        output_dir: str,
        filename_prefix: str,
        report_title: str,
        max_workers: Optional[int] = None,
        send_email: bool = False,
        email_config: Optional[EmailConfig] = None,
        recipient_email: Optional[str] = None
    ) -> str:
        """
        Run full pipeline: process topics and generate report.

        Args:
            topics: List of topics to process
            output_dir: Output directory path
            filename_prefix: Prefix for filename
            report_title: Title shown on report cover
            max_workers: Max concurrent workers
            send_email: Whether to send via email
            email_config: Email configuration (if sending)
            recipient_email: Recipient email address (if sending)

        Returns:
            Path to generated PDF file
        """
        summaries = self.run_topics(topics, max_workers)
        if all(summary.startswith(("数据处理失败:", "处理失败:")) for summary in summaries.values()):
            raise RuntimeError("全部主题检索或处理失败，未生成成功简报")
        return self.generate_report(
            summaries, output_dir, filename_prefix, report_title,
            send_email, email_config, recipient_email
        )
