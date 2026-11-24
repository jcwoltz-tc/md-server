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
OBSIDIAN_VAULT = os.environ.get('OBSIDIAN_VAULT', '')   # enables "Open in Obsidian"


# ---------------------------------------------------------------------------
# Vault index: every non-hidden file's path relative to SERVE_DIR, shortest
# first, written to a temp file that obsidian.lua reads to resolve
# [[wiki links]]. Rebuilt at most every INDEX_TTL.

_index_lock = threading.Lock()
_index = None
_index_stamp = 0      # bumped whenever the file tree actually changes
_index_built = 0.0
_index_files = []     # current index file last; previous kept for in-flight renders


def _hidden_or_outside(real_path, real_srv):
    """True if a resolved path leaves the vault or passes through a dot entry."""
    if not real_path.startswith(real_srv + os.sep):
        return True
    return any(part.startswith('.') for part in os.path.relpath(real_path, real_srv).split(os.sep))


def _get_index():
    """Return (index file path, stamp, vault-relative file list)."""
    global _index, _index_stamp, _index_built
    with _index_lock:
        now = time.monotonic()
        if _index is not None and now - _index_built < INDEX_TTL:
            return _index_files[-1], _index_stamp, _index
        srv = os.path.realpath(SERVE_DIR)
        new = []
        for root, dirs, files in os.walk(srv):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for name in files:
                if name.startswith('.'):
                    continue
                full = os.path.join(root, name)
                # A symlink is listed only when its target is a visible file
                # inside the vault; do_GET applies the same rule
                if os.path.islink(full) and _hidden_or_outside(os.path.realpath(full), srv):
                    continue
                new.append(os.path.relpath(full, srv))
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
        return _index_files[-1], _index_stamp, _index


# ---------------------------------------------------------------------------
# Backlinks: which notes link to a given note. Every .md file's link targets
# are cached by mtime, so only changed files are re-read; targets resolve the
# way obsidian.lua resolves them, so a backlink matches where the link goes.

# Scanned line by line with bounded patterns, so a malformed note (an
# unclosed [[ or fence, a huge line) costs linear time, never quadratic.
WIKILINK_RE = re.compile(r'\[\[([^\[\]|#\n]{0,500})(?:[|#][^\]\n]{0,500})?\]\]')
MDLINK_RE = re.compile(
    r'\]\(\s{0,10}(?:<([^<>\n]{1,500}?\.md)>|([^()\s<>]{1,500}?\.md))'
    r'(?:#[^()\s]{0,500})?(?:\s{1,10}"[^"\n]{0,500}")?\s{0,10}\)', re.IGNORECASE)
REFLINK_RE = re.compile(r'^\s{0,3}\[[^\]\n]{1,500}\]:\s{0,10}<?([^\s<>]{1,500}?\.md)(?:#\S*)?>?(?:\s|$)',
                        re.IGNORECASE)
FENCE_RE = re.compile(r'^\s*(`{3,}|~{3,})')
INLINE_CODE_RE = re.compile(r'`[^`\n]*`')
SCAN_MAX_BYTES = 4 * 1024 * 1024   # notes bigger than this are not scanned

_links_lock = threading.Lock()
_links = {}            # relpath -> (mtime, [('wiki', name) | ('md', path)])
_backrefs = {}         # target relpath -> [source relpath, newest first]
_backrefs_key = None   # (index stamp, links version) the map was built for
_links_version = 0
_links_checked = 0.0


def _scan_links(text):
    """Link targets in a note, skipping fenced and inline code.

    Known gap: 4-space indented code blocks are scanned, because a line
    indented that far is just as often a nested list item."""
    out = []
    fence = None
    for line in text.split('\n'):
        m = FENCE_RE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                    and not line.strip()[len(m.group(1)):].strip():
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        line = INLINE_CODE_RE.sub('', line)
        if '[[' in line:
            out += [('wiki', m.group(1).strip().rstrip('\\').strip()) for m in WIKILINK_RE.finditer(line)]
        if '](' in line:
            for m in MDLINK_RE.finditer(line):
                target = m.group(1) or m.group(2)
                if '://' not in target:
                    out.append(('md', urllib.parse.unquote(target)))
        if ']:' in line:
            m = REFLINK_RE.match(line)
            if m and '://' not in m.group(1):
                out.append(('md', urllib.parse.unquote(m.group(1))))
    return out


def _norm(dirname, rel):
    """Join and normalise a vault-relative path; None if it leaves the vault."""
    parts = []
    for seg in (dirname + '/' + rel if dirname else rel).split('/'):
        if seg == '..':
            if not parts:
                return None
            parts.pop()
        elif seg and seg != '.':
            parts.append(seg)
    return '/'.join(parts)


def _make_finder(files):
    """Python twin of find() in obsidian.lua."""
    exists = set(files)
    by_name = {}
    for p in files:   # already shortest first
        by_name.setdefault(os.path.basename(p).lower(), []).append(p)

    def find(src_dir, name):
        name = name.replace('\\', '/')
        same = _norm(src_dir, name)
        if same and same in exists:
            return same
        candidates = by_name.get(os.path.basename(name).lower(), [])
        if '/' in name:
            suffix = '/' + name.lstrip('/').lower()
            return next((p for p in candidates if ('/' + p.lower()).endswith(suffix)), None)
        base = os.path.basename(name)
        return next((p for p in candidates if os.path.basename(p) == base),
                    candidates[0] if candidates else None)
    return find, exists


def _get_backlinks(target_rel, files, stamp):
    """Notes linking to target_rel, newest first."""
    global _backrefs, _backrefs_key, _links_version, _links_checked
    srv = os.path.realpath(SERVE_DIR)
    with _links_lock:
        now = time.monotonic()
        due = now - _links_checked >= INDEX_TTL
        if due:
            _links_checked = now

    # Files are read and scanned without the lock, so a slow scan never
    # blocks other requests; results are swapped in under it
    if due:
        md = [p for p in files if p.lower().endswith('.md')]
        updates = {}
        for rel in md:
            full = os.path.join(srv, rel)
            try:
                st = os.stat(full)
            except OSError:
                continue
            cached = _links.get(rel)
            if cached and cached[0] == st.st_mtime:
                continue
            targets = []
            if st.st_size <= SCAN_MAX_BYTES:
                try:
                    with open(full, 'r', encoding='utf-8-sig', errors='replace') as f:
                        targets = _scan_links(f.read())
                except OSError:
                    continue
            updates[rel] = (st.st_mtime, targets)
        live = set(md)
        with _links_lock:
            gone = [r for r in _links if r not in live]
            for rel in gone:
                del _links[rel]
            _links.update(updates)
            if updates or gone:
                _links_version += 1

    with _links_lock:
        if _backrefs_key != (stamp, _links_version):
            find, exists = _make_finder(files)
            refs = {}
            for src, (_, targets) in _links.items():
                src_dir = os.path.dirname(src)
                for kind, name in targets:
                    if kind == 'wiki':
                        if not name:
                            continue
                        hit = find(src_dir, name + '.md')
                        if not hit and '.' in name:
                            hit = find(src_dir, name)
                    else:
                        hit = _norm('', name.lstrip('/')) if name.startswith('/') else _norm(src_dir, name)
                        hit = hit if hit in exists else None
                    if hit and hit != src:
                        refs.setdefault(hit, set()).add(src)
            _backrefs = {t: sorted(s, key=lambda r: (-_links[r][0], r)) for t, s in refs.items()}
            _backrefs_key = (stamp, _links_version)
        return _backrefs.get(target_rel, [])


def _backlinks_html(sources):
    items = []
    for rel in sources:
        url = urllib.parse.quote('/' + rel)
        name = os.path.splitext(os.path.basename(rel))[0]
        folder = os.path.dirname(rel)
        items.append(f'<li><a href="{url}">{html.escape(name)}</a>'
                     + (f' <span class="backlink-dir">{html.escape(folder)}</span>' if folder else '')
                     + '</li>')
    return (f'<div class="backlinks"><details open><summary>Linked from ({len(sources)})</summary>'
            f'<ul>{"".join(items)}</ul></details></div>')


def _parse_frontmatter(content):
    """Return (raw YAML frontmatter, offset where the body starts), or ('', 0)."""
    m = re.match(r'---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)', content, re.DOTALL)
    return (m.group(1), m.end()) if m else ('', 0)


# ---------------------------------------------------------------------------
# Render cache: (path, style, toc, meta, backlinks) -> (mtime, index_stamp, html bytes)

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
        meta_mode = False   # print the tags/related strip
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
            elif path.endswith('.meta'):
                path = path[:-5]
                meta_mode = True
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
        # A symlink must not reach a hidden file (.obsidian, .git, ...)
        if _hidden_or_outside(file_path, real_srv):
            self.send_error(404, 'File not found')
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
        index_file, index_stamp, index_list = _get_index()
        backlinks = _get_backlinks(os.path.relpath(file_path, real_srv), index_list, index_stamp)
        cache_key = (file_path, style_name, toc_mode, meta_mode, tuple(backlinks))
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

            # Page facts for the toolbar: the file rendered (a directory URL
            # may be serving its index.md) and the Obsidian vault name
            rel_url = '/' + os.path.relpath(file_path, real_srv)
            toolbar_meta = f'<meta name="md-path" content="{html.escape(rel_url)}">\n'
            if OBSIDIAN_VAULT:
                toolbar_meta += f'<meta name="md-vault" content="{html.escape(OBSIDIAN_VAULT)}">\n'
            with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
                f.write(toolbar_meta)
                toolbar_meta_tmp = f.name
            tmp_files.append(toolbar_meta_tmp)

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
                '--include-in-header=/app/props.html',
                f'--include-in-header={toolbar_meta_tmp}',
                '--include-before-body=/app/toolbar.html',
                # pagetitle sets <title> only; title would add a second H1
                f'--metadata=pagetitle:{doc_title}',
            ]

            if is_draft:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
                    f.write('<div class="draft-watermark">DRAFT</div>')
                    draft_tmp = f.name
                tmp_files.append(draft_tmp)
                cmd.append(f'--include-before-body={draft_tmp}')

            if meta_mode:
                cmd.append('--include-in-header=/app/printprops.html')

            if toc_mode:
                cmd += ['--toc', '--toc-depth=3', '--metadata=toc-title:Contents']

            if backlinks:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
                    f.write(_backlinks_html(backlinks))
                    backlinks_tmp = f.name
                tmp_files.append(backlinks_tmp)
                cmd.append(f'--include-after-body={backlinks_tmp}')

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
