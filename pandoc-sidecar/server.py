#!/usr/bin/env python3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import subprocess
import os
import re
import html
import threading
import time
import tempfile
import urllib.parse
from datetime import datetime

SERVE_DIR = '/srv'
STYLES_DIR = '/app'
PANDOC_TIMEOUT = 60   # seconds before a pandoc run is killed
INDEX_TTL = 5.0       # seconds before the filename index is rebuilt
CACHE_MAX = 128       # rendered pages kept in memory
GENERATED = b'@@MD-SERVER-GENERATED@@'   # footer timestamp, filled per request


# ---------------------------------------------------------------------------
# Vault index: every non-hidden file's path relative to SERVE_DIR, shortest
# first, written to a temp file that obsidian.lua reads to resolve
# [[wiki links]]. Rebuilt at most every INDEX_TTL.

_index_lock = threading.Lock()
_index = None
_index_stamp = 0      # bumped whenever the file tree actually changes
_index_built = 0.0
_index_files = []     # current index file last; previous kept for in-flight renders


def _get_index():
    """Return (index file path, stamp)."""
    global _index, _index_stamp, _index_built
    with _index_lock:
        now = time.monotonic()
        if _index is not None and now - _index_built < INDEX_TTL:
            return _index_files[-1], _index_stamp
        srv = os.path.realpath(SERVE_DIR)
        new = []
        for root, dirs, files in os.walk(srv):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for name in files:
                if not name.startswith('.'):
                    new.append(os.path.relpath(os.path.join(root, name), srv))
        new.sort(key=lambda p: (len(p), p))  # prefer shortest (least-nested) path
        _index_built = now
        if new != _index:
            _index = new
            _index_stamp += 1
            with tempfile.NamedTemporaryFile(mode='w', prefix='md-index-', suffix='.txt',
                                             delete=False, encoding='utf-8') as f:
                f.write('\n'.join(new) + '\n')
            _index_files.append(f.name)
            while len(_index_files) > 2:
                try:
                    os.unlink(_index_files.pop(0))
                except OSError:
                    pass
        return _index_files[-1], _index_stamp


def _parse_frontmatter(content):
    """Return (raw YAML frontmatter, offset where the body starts), or ('', 0)."""
    m = re.match(r'---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)', content, re.DOTALL)
    return (m.group(1), m.end()) if m else ('', 0)


# ---------------------------------------------------------------------------
# Render cache: (path, style, toc) -> (mtime, index_stamp, html bytes)

_cache_lock = threading.Lock()
_render_cache = {}


def _cache_get(key, mtime, stamp):
    with _cache_lock:
        entry = _render_cache.get(key)
        if entry and entry[0] == mtime and entry[1] == stamp:
            return entry[2]
    return None


def _cache_put(key, mtime, stamp, body):
    with _cache_lock:
        if len(_render_cache) >= CACHE_MAX:
            _render_cache.clear()
        _render_cache[key] = (mtime, stamp, body)


class PandocHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path == '/_assets/mermaid.min.js':
            self.serve_asset('mermaid.min.js', 'text/javascript; charset=utf-8')
            return

        # Strip known suffixes in any order
        break_mode = False
        toc_mode = False
        full_mode = False
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
                path = path[:-8]   # compact is the default; accepted for old links
                changed = True
            elif path.endswith('.full'):
                path = path[:-5]
                full_mode = True
                changed = True

        if not path.endswith('.md'):
            self.send_error(404, 'Not a markdown file')
            return

        # Hidden files and folders (.obsidian, .git, ...) are not served,
        # matching Caddy's `hide .*`
        if any(part.startswith('.') for part in path.split('/') if part):
            self.send_error(404, 'File not found')
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

        if break_mode:
            style_name = 'break.html'
        elif full_mode:
            style_name = 'nobreak.html'
        else:
            style_name = 'compact.html'
        style_file = os.path.join(STYLES_DIR, style_name)

        mtime = os.path.getmtime(file_path)
        index_file, index_stamp = _get_index()
        cache_key = (file_path, style_name, toc_mode)
        cached = _cache_get(cache_key, mtime, index_stamp)
        if cached is not None:
            self.send_html(cached)
            return

        # Read only for frontmatter; pandoc reads the file itself
        with open(file_path, 'r', encoding='utf-8-sig', errors='replace') as f:
            content = f.read().replace('\r\n', '\n')

        # Doc-meta footer. "Generated" is filled in at send time so cached
        # pages still show when they were served.
        modified = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
        filename = os.path.basename(file_path)
        footer_html = (
            f'<div class="doc-meta">'
            f'Source: {html.escape(filename)} | Modified: {modified} | '
            f'Generated: {GENERATED.decode()}'
            f'</div>'
        )

        # Frontmatter: DRAFT watermark and document title
        frontmatter, frontmatter_end = _parse_frontmatter(content)
        is_draft = bool(re.search(r'^status:\s*DRAFT', frontmatter, re.MULTILINE | re.IGNORECASE))
        title_match = re.search(r'^title:\s*(.+)', frontmatter, re.MULTILINE | re.IGNORECASE)
        if title_match:
            doc_title = title_match.group(1).strip().strip('"').strip("'")
        else:
            doc_title = os.path.splitext(filename)[0]

        tmp_files = []

        try:
            # Footer temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
                f.write(footer_html)
                footer_tmp = f.name
            tmp_files.append(footer_tmp)

            cmd = [
                'pandoc', file_path,
                '-f', 'gfm+hard_line_breaks+yaml_metadata_block+wikilinks_title_after_pipe',
                '-t', 'html5',
                '--standalone',
                '--syntax-highlighting=kate',
                '--lua-filter=/app/obsidian.lua',
                '--lua-filter=/app/callouts.lua',
                '--lua-filter=/app/mermaid.lua',
                f'--resource-path={os.path.dirname(file_path)}',
                f'--include-in-header={style_file}',
                # pagetitle sets <title> only; title would add a second H1
                f'--metadata=pagetitle:{doc_title}',
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
                '--include-after-body=/app/mermaid.html',
            ]

            env = dict(os.environ,
                       MD_INDEX=index_file,
                       MD_DOC=os.path.relpath(file_path, real_srv))
            try:
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        timeout=PANDOC_TIMEOUT, env=env)
                if result.returncode != 0 and 'YAML' in result.stderr and frontmatter_end:
                    # Obsidian tolerates frontmatter pandoc rejects (e.g. an
                    # unrendered template's `date: {{date}}`); render without it
                    print(f'bad frontmatter in {file_path}, rendering without it', flush=True)
                    cmd[1] = '-'
                    result = subprocess.run(cmd, input=content[frontmatter_end:],
                                            capture_output=True, text=True,
                                            timeout=PANDOC_TIMEOUT, env=env)
            except subprocess.TimeoutExpired:
                self.send_error(500, 'Pandoc timed out', f'No output after {PANDOC_TIMEOUT}s')
                return

        finally:
            for f in tmp_files:
                try:
                    os.unlink(f)
                except Exception:
                    pass

        if result.returncode != 0:
            # The message goes in the status line, so keep pandoc's
            # multi-line stderr in the body only
            print(f'pandoc failed for {file_path}: {result.stderr}', flush=True)
            self.send_error(500, 'Pandoc error', result.stderr)
            return

        body = result.stdout.encode('utf-8')
        _cache_put(cache_key, mtime, index_stamp, body)
        self.send_html(body)

    def send_html(self, body):
        body = body.replace(GENERATED, datetime.now().strftime('%Y-%m-%d %H:%M').encode())
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_asset(self, name, content_type):
        asset = os.path.join(STYLES_DIR, name)
        if not os.path.isfile(asset):
            self.send_error(404, 'Asset not found')
            return
        with open(asset, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'public, max-age=86400')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        print(f'{self.address_string()} - {format % args}', flush=True)


if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', 3000), PandocHandler)
    print('Pandoc sidecar listening on :3000', flush=True)
    server.serve_forever()
