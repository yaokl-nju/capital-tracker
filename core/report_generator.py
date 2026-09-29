"""PDF report generator from markdown content."""
from typing import Dict

import markdown
import datetime
from html import escape, unescape
from pathlib import Path
import os
import tempfile
import re
from urllib.parse import urlparse

from config.settings import Config


class ReportGenerator:
    """Generate PDF reports from markdown content using WeasyPrint."""

    def __init__(
        self,
        output_path: str,
        topic: str = "情报简报",
        config: Config = None
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
                margin-top: 5cm;
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
                margin-bottom: 4rem;
            }}

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

    def create_pdf(self, data: Dict[str, str]):
        """
        Generate PDF from markdown content.

        Args:
            data: Dictionary mapping topic names to markdown content
        """
        # Build HTML
        html_parts = ["<html><head><meta charset='UTF-8'></head><body>"]

        # Cover page
        html_parts.append(f'<h1 class="report-title">{escape(self.topic)}</h1>')
        html_parts.append(
            f'<div class="meta-info">生成时间: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}</div>'
        )

        md = markdown.Markdown(extensions=['tables', 'fenced_code', 'nl2br'])

        # Process each topic section
        for topic, md_text in data.items():
            html_parts.append('<section>')
            html_parts.append(f'<div class="topic-section">■ {escape(topic)}</div>')
            # LLM/news HTML is data, not report layout or a resource directive.
            safe_markdown = re.sub(r"<[^>]*>", lambda match: escape(match.group(0)),
                                   self._normalize_list_indent(md_text))
            raw_html = md.convert(safe_markdown)
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
            from weasyprint import HTML, CSS
            from weasyprint.text.fonts import FontConfiguration
            font_config = FontConfiguration()
            html_obj = HTML(string=final_html, url_fetcher=self._reject_resource)
            css_obj = CSS(string=self._get_css(), font_config=font_config)
            target = Path(self.output_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(suffix='.pdf', dir=target.parent, delete=False) as temp:
                temp_path = temp.name
            html_obj.write_pdf(temp_path, stylesheets=[css_obj], font_config=font_config)
            if Path(temp_path).stat().st_size == 0:
                raise RuntimeError('PDF renderer produced an empty file')
            os.replace(temp_path, target)
            print(f"✅ PDF 生成成功: {self.output_path}")
        except Exception as exc:
            raise RuntimeError('PDF 生成失败，请检查 WeasyPrint 和系统 Pango 依赖') from exc
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
