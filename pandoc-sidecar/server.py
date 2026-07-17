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


IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico'}
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.ogv', '.mov'}
AUDIO_EXTENSIONS = {'.mp3', '.ogg', '.wav', '.flac', '.m4a'}


# ---------------------------------------------------------------------------
# Filename index: lowercase basename -> paths, rebuilt at most every INDEX_TTL.
# Replaces per-link recursive globs over the whole vault.

_index_lock = threading.Lock()
_index = None
_index_stamp = 0      # bumped whenever the file tree actually changes
_index_built = 0.0


def _get_index():
    global _index, _index_stamp, _index_built
    with _index_lock:
        now = time.monotonic()
        if _index is not None and now - _index_built < INDEX_TTL:
            return _index, _index_stamp
        srv = os.path.realpath(SERVE_DIR)
        new = {}
        for root, dirs, files in os.walk(srv):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for name in files:
                if name.startswith('.'):
                    continue
                new.setdefault(name.lower(), []).append(os.path.join(root, name))
        for paths in new.values():
            paths.sort(key=len)  # prefer shortest (least-nested) path
        _index_built = now
        if new != _index:
            _index = new
            _index_stamp += 1
        return _index, _index_stamp


def _find_file(filename, file_dir, srv_dir):
    """Resolve a filename (optionally with a subpath) to an absolute path
    within srv_dir. Search order: same directory, exact-case match anywhere,
    case-insensitive fallback. Returns the absolute path or None."""
    # 1. Same directory (guard against ../ escaping the vault)
    same_dir = os.path.realpath(os.path.join(file_dir, filename))
    if same_dir.startswith(srv_dir + os.sep) and os.path.isfile(same_dir):
        return same_dir

    index, _ = _get_index()
    base = os.path.basename(filename.replace('\\', '/'))
    candidates = index.get(base.lower(), [])

    if '/' in filename:
        # [[folder/note]] style: match on path suffix
        suffix = '/' + filename.replace('\\', '/').lower().lstrip('/')
        candidates = [p for p in candidates if p.lower().endswith(suffix)]
    else:
        exact = [p for p in candidates if os.path.basename(p) == base]
        if exact:
            candidates = exact

    return candidates[0] if candidates else None


def _make_url(abs_path, srv_dir):
    """Build a URL-encoded path relative to srv_dir."""
    rel = os.path.relpath(abs_path, srv_dir).replace(os.sep, '/')
    return '/' + urllib.parse.quote(rel)


def _gfm_anchor(section):
    """Emulate pandoc's gfm_auto_identifiers: lowercase, drop punctuation,
    spaces to hyphens."""
    s = section.strip().lower()
    s = re.sub(r'[^\w\- ]', '', s)
    return '#' + s.replace(' ', '-')


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
            safe = html.escape(filename, quote=True)
            return f'<span class="wiki-link-missing" title="Not found: {safe}">{safe}</span>'

        url = _make_url(found, srv_dir)
        found_ext = os.path.splitext(found)[1].lower()

        if found_ext in IMAGE_EXTENSIONS:
            # Parse dimensions from alt: "300", "300x200"
            dim_match = re.match(r'^(\d+)(?:x(\d+))?$', alt)
            if dim_match:
                w = dim_match.group(1)
                h = dim_match.group(2)
                style = f'width:{w}px;' + (f'height:{h}px;' if h else '')
                return f'<img src="{url}" alt="{html.escape(filename, quote=True)}" style="{style}">'
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
            anchor = _gfm_anchor(section)
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
        return (f'<span class="wiki-link-missing" '
                f'title="Not found: {html.escape(filename, quote=True)}">'
                f'{html.escape(label)}</span>')

    # Process embeds first, then links
    content = re.sub(r'!\[\[([^\]]+?)\]\]', resolve_embed, content)
    content = re.sub(r'\[\[([^\]]+?)\]\]', replace_link, content)
    return content


# ---------------------------------------------------------------------------
# Preprocessing that must skip code: wiki links and manual page breaks would
# otherwise be rewritten inside fenced blocks and inline code spans.

_FENCE_OPEN = re.compile(r'^[ \t]{0,3}(`{3,}|~{3,})')
_INLINE_CODE = re.compile(r'(`+[^`\n]*`+)')
_PAGEBREAK = re.compile(r'(?im)^[ \t]*(?:\\newpage|<!--\s*pagebreak\s*-->)[ \t]*$')


def _apply_outside_code(content, fn):
    """Apply fn to the text outside fenced code blocks and inline code spans."""
    lines = content.split('\n')
    in_code = []
    fence_close = None
    for line in lines:
        if fence_close is None:
            m = _FENCE_OPEN.match(line)
            in_code.append(bool(m))
            if m:
                marker = m.group(1)
                fence_close = re.compile(
                    r'^[ \t]{0,3}' + re.escape(marker[0]) + '{' + str(len(marker)) + r',}[ \t]*$')
        else:
            in_code.append(True)
            if fence_close.match(line):
                fence_close = None

    out = []
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and in_code[j] == in_code[i]:
            j += 1
        blob = '\n'.join(lines[i:j])
        if not in_code[i]:
            pieces = _INLINE_CODE.split(blob)
            blob = ''.join(p if k % 2 else fn(p) for k, p in enumerate(pieces))
        out.append(blob)
        i = j
    return '\n'.join(out)


def _parse_frontmatter(content):
    """Return the raw YAML frontmatter block, or '' if there is none."""
    m = re.match(r'---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)', content, re.DOTALL)
    return m.group(1) if m else ''


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
        _, index_stamp = _get_index()
        cache_key = (file_path, style_name, toc_mode)
        cached = _cache_get(cache_key, mtime, index_stamp)
        if cached is not None:
            self.send_html(cached)
            return

        # Read and preprocess
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        def preprocess(text):
            text = resolve_wiki_links(text, file_path, real_srv)
            return _PAGEBREAK.sub('\n<div class="page-break"></div>\n', text)

        content = _apply_outside_code(content, preprocess)

        # Doc-meta footer
        modified = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
        generated = datetime.now().strftime('%Y-%m-%d %H:%M')
        filename = os.path.basename(file_path)
        footer_html = (
            f'<div class="doc-meta">'
            f'Source: {html.escape(filename)} | Modified: {modified} | Generated: {generated}'
            f'</div>'
        )

        # Frontmatter: DRAFT watermark and document title
        frontmatter = _parse_frontmatter(content)
        is_draft = bool(re.search(r'^status:\s*DRAFT', frontmatter, re.MULTILINE | re.IGNORECASE))
        title_match = re.search(r'^title:\s*(.+)', frontmatter, re.MULTILINE | re.IGNORECASE)
        if title_match:
            doc_title = title_match.group(1).strip().strip('"').strip("'")
        else:
            doc_title = os.path.splitext(filename)[0]

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
                '-f', 'gfm+hard_line_breaks+yaml_metadata_block',
                '-t', 'html5',
                '--standalone',
                '--syntax-highlighting=kate',
                '--lua-filter=/app/callouts.lua',
                '--lua-filter=/app/mermaid.lua',
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
                '--include-after-body=/app/mermaid.html',
            ]

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=PANDOC_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.send_error(500, f'Pandoc timed out after {PANDOC_TIMEOUT}s')
                return

        finally:
            for f in tmp_files:
                try:
                    os.unlink(f)
                except Exception:
                    pass

        if result.returncode != 0:
            self.send_error(500, f'Pandoc error: {result.stderr}')
            return

        body = result.stdout.encode('utf-8')
        _cache_put(cache_key, mtime, index_stamp, body)
        self.send_html(body)

    def send_html(self, body):
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
