"""Single-document renderer isolated from the research process and its credentials."""
import json
import sys
from urllib.parse import urlparse


def reject_resource(url, *args, **kwargs):
    raise ValueError('Report resource loading is disabled: ' + urlparse(url).scheme)


def render_pdf(html, css, target):
    from weasyprint import HTML, CSS
    from weasyprint.text.fonts import FontConfiguration
    font_config = FontConfiguration()
    HTML(string=html, url_fetcher=reject_resource).write_pdf(
        target, stylesheets=[CSS(string=css, font_config=font_config)], font_config=font_config)


def main():
    if len(sys.argv) != 2:
        raise ValueError('PDF worker requires one output path')
    text = sys.stdin.buffer.read(64 * 1024 * 1024 + 1)
    if len(text) > 64 * 1024 * 1024:
        raise ValueError('PDF input exceeds 64 MiB')
    payload = json.loads(text.decode('utf-8'))
    if (not isinstance(payload, dict) or not isinstance(payload.get('html'), str) or
            not isinstance(payload.get('css'), str)):
        raise ValueError('PDF worker requires HTML and CSS text')
    render_pdf(payload['html'], payload['css'], sys.argv[1])


if __name__ == '__main__':
    main()
