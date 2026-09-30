"""Orchestrator for running multiple trackers."""
import os
import datetime
import asyncio
import hashlib
from typing import List, Optional

from .tracker import Tracker
from .report_generator import ReportGenerator
from .email_service import EmailConfig, EmailService
from config.settings import Config
from .artifacts import save_report_artifacts, record_render_status
from .evidence_quality import coverage_summary, evidence_index, topic_notice
from .checkpoint_recovery import save_batch_manifest


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
        max_workers: Optional[int] = None,
        on_topic=None
    ) -> dict[str, str]:
        """
        Process multiple topics in parallel.

        Args:
            topics: List of topics to process
            max_workers: Max concurrent workers (default from config)

        Returns:
            Dictionary mapping topic to summary
        """
        return asyncio.run(self.run_topics_async(topics, max_workers, on_topic=on_topic))

    async def run_topics_async(self, topics, max_workers=None, on_topic=None):
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
                except Exception as exc:
                    summary = f"处理失败: {type(exc).__name__}"
                if on_topic is not None:
                    try:
                        await asyncio.to_thread(on_topic, topic, summary)
                    except Exception as exc:
                        print(f'⚠️ [{topic}] 检查点保存失败: {type(exc).__name__}；继续生成最终报告')
                return topic, summary

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

        # Preserve analysis and evidence even when PDF rendering fails.
        snapshot = getattr(self.tracker, 'snapshot_records', None)
        records = snapshot() if snapshot is not None else {}
        archive = save_report_artifacts(output_path, report_title, summaries, records, self.config)

        # Generate PDF
        gen = ReportGenerator(output_path, topic=report_title, config=self.config)
        try:
            gen.create_pdf(summaries, overview=coverage_summary(summaries, records),
                           topic_notices={topic: topic_notice(record) for topic, record in records.items()},
                           source_urls={topic: [item['url'] for item in evidence_index(record.get('raw_evidence', ''))]
                                        for topic, record in records.items()} if records else None)
        except BaseException as exc:
            record_render_status(archive, 'failed', type(exc).__name__)
            raise
        record_render_status(archive, 'completed')

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
        on_topic = None
        topics = list(dict.fromkeys(t.strip() for t in topics if isinstance(t, str) and t.strip()))
        if not topics:
            raise ValueError('At least one non-empty topic is required')
        if self.config.ENABLE_TOPIC_CHECKPOINTS:
            stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            checkpoint_dir = os.path.join(output_dir, '.checkpoints', f'{filename_prefix}_{stamp}')
            os.makedirs(checkpoint_dir, exist_ok=True)
            save_batch_manifest(checkpoint_dir, report_title, topics)

            def on_topic(topic, summary):
                digest = hashlib.sha256(topic.encode('utf-8')).hexdigest()[:24]
                path = os.path.join(checkpoint_dir, f'topic_{digest}.pdf')
                single_snapshot = getattr(self.tracker, 'snapshot_record', None)
                if single_snapshot:
                    records = {topic: single_snapshot(topic)}
                else:
                    snapshot = getattr(self.tracker, 'snapshot_records', None)
                    records = snapshot() if snapshot else {}
                save_report_artifacts(path, report_title + '（单主题检查点）',
                                      {topic: summary}, records, self.config, render_status='not_requested')

        summaries = self.run_topics(topics, max_workers, on_topic=on_topic)
        if all(summary.startswith(("数据处理失败:", "处理失败:")) for summary in summaries.values()):
            os.makedirs(output_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            path = os.path.join(output_dir, f'{filename_prefix}_{stamp}_failed.pdf')
            snapshot = getattr(self.tracker, 'snapshot_records', None)
            archive = save_report_artifacts(path, report_title, summaries,
                                            snapshot() if snapshot else {}, self.config, render_status='not_requested')
            raise RuntimeError(f"全部主题检索或处理失败，未生成成功简报；诊断已保存: {archive}")
        return self.generate_report(
            summaries, output_dir, filename_prefix, report_title,
            send_email, email_config, recipient_email
        )
