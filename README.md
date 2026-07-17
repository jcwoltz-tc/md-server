# md-server

A self-hosted markdown rendering server using Caddy and Pandoc. Drop markdown files in a directory and browse them as styled HTML with file browsing, table of contents, callouts, code copy buttons, and print-friendly output.

## Quick Start

```bash
git clone <this-repo> md-server
cd md-server
mkdir -p docs   # put your .md files here
docker compose up -d
```

Browse to **http://localhost:8095**

## Serving a Different Directory

By default, files are served from `./docs/`. To serve markdown from elsewhere, create a `.env` file:

```
DOCS_DIR=/path/to/your/markdown
```

Or inline:

```bash
DOCS_DIR=~/notes docker compose up -d
```

## Features

### Rendering Modes

Append suffixes to any `.md` URL to change the output:

| Suffix | Effect |
|--------|--------|
| *(none)* | Compact layout (default), continuous flow |
| `.full` | Roomier layout, larger fonts |
| `.break` | Page break before each H2 (for printing) |
| `.toc` | Adds a table of contents |
| `.compact` | Accepted for old links (same as default) |

Suffixes combine: `doc.md.toc.break` gives you a TOC with page breaks.

All modes follow your OS/browser dark-mode preference on screen; printed output always stays light.

### Callouts

GitHub-style alert syntax is supported:

```markdown
> [!NOTE]
> This renders as a styled callout box.
```

Supported types: the 5 GitHub alerts (`NOTE`, `TIP`, `IMPORTANT`, `WARNING`, `CAUTION`) plus Obsidian's extended set (`info`, `todo`, `abstract`, `quote`, `success`, `example`, `question`, `failure`, `danger`, `bug`, and their aliases).

### Mermaid Diagrams

Fenced code blocks with the `mermaid` language are rendered as diagrams client-side (mermaid.js is bundled into the sidecar image — no CDN needed at view time):

````markdown
```mermaid
graph LR
  A --> B
```
````

### Wiki Links

Obsidian-style `[[wiki links]]` are resolved against the served directory. Supports `[[file]]`, `[[file|display text]]`, and `[[file#heading]]`.

### Other

- File browser for navigating directories; a directory containing `index.md` or `README.md` renders it instead of the raw listing
- Automatic dark mode (`prefers-color-scheme`)
- Copy button on code blocks
- DRAFT watermark when frontmatter contains `status: DRAFT`
- Footer with source filename and timestamps
- Syntax highlighting via Pandoc (kate theme)

## Architecture

Two containers:

- **caddy** — serves the file browser and proxies `.md` requests to the sidecar
- **pandoc-sidecar** — a Python HTTP server that reads the markdown file, resolves wiki links, and runs Pandoc to produce styled HTML

## Changing the Port

Edit the port mapping in `docker-compose.yml`:

```yaml
ports:
  - "8095:80"  # change 8095 to whatever you want
```

## Security

This server has no authentication and serves over plain HTTP — intended for local or trusted-network use.

Since it already uses Caddy, you can add security directly in the Caddyfile:

- **Basic auth:** add a `basicauth` block ([Caddy docs](https://caddyserver.com/docs/caddyfile/directives/basicauth))
- **HTTPS:** replace `:80` with your domain and remove `auto_https off` — Caddy handles certificates automatically
- Or put it behind [Tailscale](https://tailscale.com/), [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/), etc.

## License

[Unlicense](LICENSE) — public domain. Do whatever you want with it.
