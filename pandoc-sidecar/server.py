#!/usr/bin/env python3
from http.server import HTTPServer, BaseHTTPRequestHandler
import subprocess
import os
import re
import glob
import tempfile
import urllib.parse
from datetime import datetime

SERVE_DIR = '/srv'
STYLES_DIR = '/app'


IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico'}
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.ogv', '.mov'}
AUDIO_EXTENSIONS = {'.mp3', '.ogg', '.wav', '.flac', '.m4a'}


def _find_file(filename, file_dir, srv_dir):
    """Resolve a filename to an absolute path within srv_dir.

    Search order: same directory, recursive exact match, case-insensitive fallback.
    Returns the absolute path or None.
    """
    # 1. Same directory
    same_dir = os.path.join(file_dir, filename)
    if os.path.exists(same_dir):
        return same_dir

    # 2. Recursive search (exact name)
    matches = glob.glob(os.path.join(srv_dir, '**', filename), recursive=True)

    # 3. Case-insensitive fallback
    if not matches:
        target_lower = filename.lower()
        ext = os.path.splitext(filename)[1]
        pattern = '*' + ext if ext else '*'
        matches = [
            f for f in glob.glob(os.path.join(srv_dir, '**', pattern), recursive=True)
            if os.path.basename(f).lower() == target_lower
        ]

    if matches:
        matches.sort(key=len)  # prefer shortest (least-nested) path
        return matches[0]

    return None


def _make_url(abs_path, srv_dir):
    """Build a URL-encoded path relative to srv_dir."""
    rel = os.path.relpath(abs_path, srv_dir).replace(os.sep, '/')
    return '/' + urllib.parse.quote(rel)


def resolve_wiki_links(content, file_path, srv_dir):
    """Convert Obsidian [[wiki links]] and ![[embeds]] to HTML, resolved against the vault."""
    file_dir = os.path.dirname(file_path)

    def resolve_embed(m):
        inner = m.group(1)

        # Split on | for alt text / dimensions: ![[image.png|300]] or ![[image.png|alt text]]
        if '|' in inner:
            target_part, alt = inner.split('|', 1)
            alt = alt.strip()
        else:
            target_part = inner
            alt = ''

        filename = target_part.strip()
        if not filename:
            return m.group(0)

        ext = os.path.splitext(filename)[1].lower()

        # Try to find the file (with extension as-is, then .md fallback)
        found = _find_file(filename, file_dir, srv_dir)
        if not found and not ext:
            found = _find_file(filename + '.md', file_dir, srv_dir)

        if not found:
            return f'<span class="wiki-link-missing" title="Not found: {filename}">{filename}</span>'

        url = _make_url(found, srv_dir)
        found_ext = os.path.splitext(found)[1].lower()

        if found_ext in IMAGE_EXTENSIONS:
            # Parse dimensions from alt: "300", "300x200"
            dim_match = re.match(r'^(\d+)(?:x(\d+))?$', alt)
            if dim_match:
                w = dim_match.group(1)
                h = dim_match.group(2)
                style = f'width:{w}px;' + (f'height:{h}px;' if h else '')
                return f'<img src="{url}" alt="{filename}" style="{style}">'
            alt_text = alt if alt else filename
            return f'![{alt_text}]({url})'

        if found_ext in VIDEO_EXTENSIONS:
            return f'<video controls src="{url}"></video>'

        if found_ext in AUDIO_EXTENSIONS:
            return f'<audio controls src="{url}"></audio>'

        if found_ext == '.pdf':
            return f'<iframe src="{url}" style="width:100%;height:600px;border:none;"></iframe>'

        # Non-media file — link to it
        label = alt if alt else filename
        return f'[{label}]({url})'

    def replace_link(m):
        inner = m.group(1)

        # Split on | for display text: [[target|label]] or [[target]]
        if '|' in inner:
            target_part, display = inner.split('|', 1)
        else:
            target_part = inner
            display = None

        # Split on # for section anchors: [[file#heading]]
        if '#' in target_part:
            filename, section = target_part.split('#', 1)
            anchor = '#' + re.sub(r'\s+', '-', section.strip().lower())
        else:
            filename = target_part
            anchor = ''

        filename = filename.strip()
        label = display.strip() if display else target_part.strip()

        if not filename:
            return f'[{label}]({anchor})'

        # Try .md first, then exact filename (for non-md files)
        found = _find_file(filename + '.md', file_dir, srv_dir)
        if not found and '.' in filename:
            found = _find_file(filename, file_dir, srv_dir)

        if found:
            url = _make_url(found, srv_dir)
            return f'[{label}]({url}{anchor})'

        # Not found — render as struck-through text with tooltip
        return f'<span class="wiki-link-missing" title="Not found: {filename}">{label}</span>'

    # Process embeds first, then links
    content = re.sub(r'!\[\[([^\]]+?)\]\]', resolve_embed, content)
    content = re.sub(r'\[\[([^\]]+?)\]\]', replace_link, content)
    return content


class PandocHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        # Strip known suffixes in any order
        break_mode = False
        toc_mode = False
        compact_mode = False
        changed = True
        while changed:
            changed = False
            if path.endswith('.break'):
                path = path[:-6]
                break_mode = True
                changed = True
            elif path.endswith('.toc'):
                path = path[:-4]
                toc_mode = True
                changed = True
            elif path.endswith('.compact'):
                path = path[:-8]
                compact_mode = True
                changed = True

        if not path.endswith('.md'):
            self.send_error(404, 'Not a markdown file')
            return

        # Prevent path traversal
        real_srv = os.path.realpath(SERVE_DIR)
        file_path = os.path.realpath(os.path.join(real_srv, path.lstrip('/')))
        if not file_path.startswith(real_srv + os.sep):
            self.send_error(403, 'Forbidden')
            return

        if not os.path.isfile(file_path):
            self.send_error(404, 'File not found')
            return

        # Read and preprocess
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        content = resolve_wiki_links(content, file_path, real_srv)

        # Manual page breaks: a line containing just \newpage or <!-- pagebreak -->
        content = re.sub(
            r'(?im)^[ \t]*(?:\\newpage|<!--\s*pagebreak\s*-->)[ \t]*$',
            '\n<div class="page-break"></div>\n',
            content,
        )

        # Doc-meta footer
        mtime = os.path.getmtime(file_path)
        modified = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
        generated = datetime.now().strftime('%Y-%m-%d %H:%M')
        filename = os.path.basename(file_path)
        footer_html = (
            f'<div class="doc-meta">'
            f'Source: {filename} | Modified: {modified} | Generated: {generated}'
            f'</div>'
        )

        # DRAFT watermark from frontmatter
        is_draft = bool(re.search(r'^status:\s*DRAFT', content, re.MULTILINE | re.IGNORECASE))

        # Document title: prefer frontmatter title, fall back to filename
        title_match = re.search(r'^title:\s*(.+)', content, re.MULTILINE | re.IGNORECASE)
        if title_match:
            doc_title = title_match.group(1).strip().strip('"').strip("'")
        else:
            doc_title = os.path.splitext(filename)[0]

        if compact_mode:
            style_name = 'compact.html'
        elif break_mode:
            style_name = 'break.html'
        else:
            style_name = 'nobreak.html'
        style_file = os.path.join(STYLES_DIR, style_name)
        tmp_files = []

        try:
            # Preprocessed content as temp .md
            with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
                f.write(content)
                content_tmp = f.name
            tmp_files.append(content_tmp)

            # Footer temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
                f.write(footer_html)
                footer_tmp = f.name
            tmp_files.append(footer_tmp)

            cmd = [
                'pandoc', content_tmp,
                '-f', 'gfm+hard_line_breaks',
                '-t', 'html5',
                '--standalone',
                '--syntax-highlighting=kate',
                '--lua-filter=/app/callouts.lua',
                f'--resource-path={os.path.dirname(file_path)}',
                f'--include-in-header={style_file}',
                f'--metadata=title:{doc_title}',
            ]

            if is_draft:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
                    f.write('<div class="draft-watermark">DRAFT</div>')
                    draft_tmp = f.name
                tmp_files.append(draft_tmp)
                cmd.append(f'--include-before-body={draft_tmp}')

            if toc_mode:
                cmd += ['--toc', '--toc-depth=3', '--metadata=toc-title:Contents']

            cmd += [
                f'--include-after-body={footer_tmp}',
                '--include-after-body=/app/copycode.html',
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)

        finally:
            for f in tmp_files:
                try:
                    os.unlink(f)
                except Exception:
                    pass

        if result.returncode != 0:
            self.send_error(500, f'Pandoc error: {result.stderr}')
            return

        html = result.stdout.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, format, *args):
        print(f'{self.address_string()} - {format % args}', flush=True)


if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', 3000), PandocHandler)
    print('Pandoc sidecar listening on :3000', flush=True)
    server.serve_forever()
