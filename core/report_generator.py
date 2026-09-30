"""PDF report generator from markdown content."""
from typing import Dict

import markdown
import datetime
from html import escape, unescape
from pathlib import Path
import os
import tempfile
import re
import json
import sys
import math
import subprocess
from urllib.parse import urlparse

from config.settings import Config


class ReportGenerator:
    """Generate PDF reports from markdown content using WeasyPrint."""

    def __init__(
        self,
        output_path: str,
        topic: str = "情报简报",
        config: Config = None,
        source_generated_at: str = None
    ):
        """
        Initialize report generator.

        Args:
            output_path: Full path for output PDF
            topic: Report title/topic
            config: Configuration object
        """
        self.output_path = output_path
        self.topic = topic
        self.config = config or Config()
        self.source_generated_at = source_generated_at
        self.font_name = "Kaiti, PingFang SC, Noto Sans CJK SC, sans-serif"

    def _get_css(self) -> str:
        """Get CSS styles for PDF generation."""
        return f"""
            /* 引入 Mac 系统字体 */
            body {{
                font-family: {self.font_name};
                font-size: 11pt;
                line-height: 1.6;
                color: #333;
            }}

            /* 确保链接继承字体 */
            a {{
                color: #007bff;
                text-decoration: none;
                font-family: {self.font_name};
            }}

            /* 核心：分页控制 */
            @page {{
                size: A4;
                margin: 2.5cm;

                /* 定义页脚：显示页码 */
                @bottom-center {{
                    content: "Page " counter(page);
                    font-size: 10pt;
                    color: #888;
                }}
            }}

            /* 封面单独设置：不需要页眉页脚 */
            @page :first {{
                margin-top: 3cm;
                @bottom-center {{ content: ""; }}
            }}

            h1.report-title {{
                font-size: 26pt;
                text-align: center;
                color: #2c3e50;
                margin-bottom: 20px;
                break-after: avoid;
            }}

            .meta-info {{
                text-align: center;
                color: #7f8c8d;
                margin-bottom: 2rem;
            }}
            .topic-notice {{ font-size: 9pt; color: #606c76; margin: 8px 0 18px; }}
            .frontmatter-page {{ break-after: page; }}
            .toc {{ margin: 24px 0; }}
            .toc h2 {{ font-size: 14pt; }}
            .toc ol {{ columns: 2; column-gap: 24px; padding-left: 0; list-style: none; }}
            .toc li {{ font-size: 9pt; line-height: 1.6; margin-bottom: 8px; break-inside: avoid; }}
            .toc a::after {{ content: leader('.') target-counter(attr(href), page); color: #666; }}
            .topic-section {{ bookmark-level: 1; }}
            .content h1 {{ bookmark-level: 2; }}
            .content h2 {{ bookmark-level: 3; }}
            .content h3 {{ bookmark-level: 4; }}
            .coverage th:first-child, .coverage td:first-child {{ width: 32%; }}
            .coverage th, .coverage td {{ box-sizing: border-box; }}
            .coverage th:nth-child(2), .coverage td:nth-child(2) {{ width: 18%; }}
            .coverage th:nth-child(n+3), .coverage td:nth-child(n+3) {{ width: 12.5%; }}

            /* 板块样式 */
            .topic-section {{
                background-color: #f4f6f9;
                border-left: 6px solid #2980b9;
                padding: 15px;
                margin-top: 30px;
                margin-bottom: 20px;
                font-size: 16pt;
                font-weight: bold;
                color: #2c3e50;
                break-after: avoid;
            }}

            h2 {{
                border-bottom: 1px solid #eee;
                padding-bottom: 5px;
                margin-top: 25px;
                color: #34495e;
                break-after: avoid;
            }}

            h3 {{
                color: #2c3e50;
                break-after: avoid;
            }}

            p {{ margin-bottom: 10px; text-align: justify; overflow-wrap: anywhere; }}
            li {{ overflow-wrap: anywhere; }}

            pre {{
                background: #282c34;
                color: #abb2bf;
                padding: 10px;
                border-radius: 4px;
                font-family: Monaco, monospace;
                font-size: 0.9em;
                white-space: pre-wrap;
                break-inside: avoid;
            }}

            /* 表格美化 */
            table {{
                width: 100%;
                table-layout: fixed;
                overflow-wrap: anywhere;
                font-size: 9pt;
                border-collapse: collapse;
                margin: 15px 0;
            }}
            th, td {{
                border: 1px solid #ddd;
                padding: 6px;
                text-align: left;
            }}
            th {{ background-color: #f2f2f2; }}
            thead {{ display: table-header-group; }}
            tr {{ break-inside: avoid; }}
            .date {{ white-space: nowrap; font-size: 8pt; }}
            a {{ overflow-wrap: anywhere; }}


            ul {{
                padding-left: 20px;
            }}
        """

    @staticmethod
    def _format_table_dates(match):
        # Change text nodes only; dates embedded in source URLs must stay intact.
        parts = re.split(r'(<[^>]+>)', match.group(1))
        for i in range(0, len(parts), 2):
            parts[i] = re.sub(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)',
                              lambda date: f'<span class="date">{date.group(0)}</span>', parts[i])
        return '<td>' + ''.join(parts) + '</td>'

    @staticmethod
    def _escape_raw_html(text):
        """Escape markup while preserving Markdown's HTTP angle destinations/autolinks."""
        def escape_tag(match):
            token = match[0]
            if re.fullmatch(r'<https?://[^\s<>]+>', token, flags=re.I):
                return token
            return escape(token)
        return re.sub(r'<[^>]*>', escape_tag, text)

    @staticmethod
    def _safe_href(match):
        try:
            url = urlparse(unescape(match.group(1)))
            if url.scheme in ('http', 'https') and url.hostname and not url.username and not url.password:
                return match.group(0)
        except ValueError:
            pass
        return ''

    @staticmethod
    def _reject_resource(url, *args, **kwargs):
        # The report needs hyperlinks, never remote images, CSS or local files.
        raise ValueError(f"报告不加载外部资源: {urlparse(url).scheme}")

    @staticmethod
    def _validate_rendered_links(html, allowed_urls):
        from .search_service import canonical_url
        allowed = {key for url in allowed_urls if (key := canonical_url(url))}
        def validate(match):
            url = canonical_url(unescape(match[1]))
            return match[0] if url and url in allowed else match[2] + '（来源链接未核实）'
        return re.sub(r'<a\b[^>]*href="([^"]*)"[^>]*>(.*?)</a>', validate, html, flags=re.S)

    @staticmethod
    def _normalize_list_indent(text):
        """Accept common model list indentation without changing code or 4-space lists."""
        lines, fence, parent = [], None, False
        for line in text.splitlines(keepends=True):
            marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
            if marker:
                if fence is None:
                    fence = marker[1]
                    parent = False
                elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                    fence = None
                lines.append(line)
                continue
            if fence is None:
                if re.match(r'^(?:[-*+] |\d+\. )', line):
                    parent = True
                elif line.strip() and not line.startswith((' ', '\t')):
                    parent = False
                if parent:
                    line = re.sub(r'^ {2,3}(?=[-*+] |\d+\. )', '    ', line)
            lines.append(line)
        return ''.join(lines)

    def _render_html(self, html, target):
        timeout = self.config.PDF_RENDER_TIMEOUT
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('PDF_RENDER_TIMEOUT must be a positive finite number')
        # Native font/rendering crashes must not take the collected research with them.
        # The worker needs runtime/font settings, never API keys or email credentials.
        names = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'XDG_CACHE_HOME',
                 'FONTCONFIG_FILE', 'FONTCONFIG_PATH', 'DYLD_LIBRARY_PATH',
                 'DYLD_FALLBACK_LIBRARY_PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR')
        env = {name: os.environ[name] for name in names if name in os.environ}
        worker = Path(__file__).with_name('pdf_worker.py').resolve()
        try:
            process = subprocess.run([sys.executable, str(worker), str(Path(target).resolve())],
                                     input=json.dumps({'html': html, 'css': self._get_css()}, ensure_ascii=False),
                                     text=True, encoding='utf-8', errors='replace',
                                     capture_output=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f'PDF 渲染超过 {timeout:g} 秒；分析及证据档案已保留') from exc
        if process.returncode:
            code = f'信号 {-process.returncode}' if process.returncode < 0 else f'代码 {process.returncode}'
            raise RuntimeError(f'PDF 渲染进程异常退出（{code}）；分析及证据档案已保留')

    def create_pdf(self, data: Dict[str, str], overview: str = '', source_urls=None, topic_notices=None):
        """
        Generate PDF from markdown content.

        Args:
            data: Dictionary mapping topic names to markdown content
        """
        # Build HTML
        html_parts = ["<html><head><meta charset='UTF-8'></head><body>"]
        html_parts.append('<div class="frontmatter-page">' if len(data) >= 8 else '<div>')

        # Cover page
        html_parts.append(f'<h1 class="report-title">{escape(self.topic)}</h1>')
        html_parts.append(
            f'<div class="meta-info">生成时间: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}</div>'
        )
        if self.source_generated_at is not None:
            original_time = self.source_generated_at
            try:
                original_time = datetime.datetime.fromisoformat(original_time).astimezone().strftime('%Y-%m-%d %H:%M %Z')
            except (ValueError, OverflowError, OSError):
                pass
            html_parts.append('<p class="replay-info">离线重生成：本次仅重排已有分析，未重新检索。'
                              f'原始报告生成时间：{escape(original_time)}</p>')
        if len(data) > 1:
            html_parts.append('<nav class="toc"><h2>机构目录</h2><ol>')
            for number, name in enumerate(data, 1):
                html_parts.append(f'<li><a href="#topic-{number}">{escape(name)}</a></li>')
            html_parts.append('</ol></nav>')
        html_parts.append('</div>')

        md = markdown.Markdown(extensions=['tables', 'fenced_code', 'nl2br'])
        if overview:
            safe_overview = self._escape_raw_html(overview)
            html_parts.append('<section class="coverage"><h2 class="topic-section">资料覆盖概览</h2>'
                              f'<div class="content">{self._validate_rendered_links(md.convert(safe_overview), [])}</div></section>')
            md.reset()

        # Process each topic section
        for number, (topic, md_text) in enumerate(data.items(), 1):
            html_parts.append('<section>')
            html_parts.append(f'<h2 class="topic-section" id="topic-{number}">■ {escape(topic)}</h2>')
            if topic_notices and topic in topic_notices:
                html_parts.append(f'<p class="topic-notice">{escape(topic_notices[topic])}</p>')
            # LLM/news HTML is data, not report layout or a resource directive.
            safe_markdown = self._escape_raw_html(self._normalize_list_indent(md_text))
            raw_html = md.convert(safe_markdown)
            if source_urls is not None:
                raw_html = self._validate_rendered_links(raw_html, source_urls.get(topic, []))
            raw_html = re.sub(r'href="([^"]*)"', self._safe_href, raw_html)
            raw_html = re.sub(r"<td>(.*?)</td>", self._format_table_dates, raw_html, flags=re.S)
            html_parts.append(f'<div class="content">{raw_html}</div>')
            html_parts.append('</section>')
            # Reset markdown for next iteration
            md.reset()

        html_parts.append("</body></html>")
        final_html = "".join(html_parts)

        # Import only when rendering; CLI/search remain usable without native libs.
        temp_path = None
        try:
            target = Path(self.output_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(suffix='.pdf', dir=target.parent, delete=False) as temp:
                temp_path = temp.name
            self._render_html(final_html, temp_path)
            if Path(temp_path).stat().st_size == 0:
                raise RuntimeError('PDF renderer produced an empty file')
            os.replace(temp_path, target)
            print(f"✅ PDF 生成成功: {self.output_path}")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError('PDF 生成失败，请检查 WeasyPrint 和系统 Pango 依赖') from exc
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
