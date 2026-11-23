-- Lua filter: Obsidian syntax that pandoc's gfm reader doesn't render.
--
--   [[note]] [[note|label]] [[note#heading]]   resolved against the vault
--   ![[image.png|300]] ![[clip.mp4]] ![[doc.pdf]]   embeds
--   ==highlight==                              <mark>
--   ==🟢highlight== (🔴🟠🟡🟢🔵🟣)              <mark class="hl-green">
--   \newpage  /  <!-- pagebreak -->            manual page break
--   frontmatter tags: / related:               strip under the H1
--   #tag in the body                           <span class="doc-tag inline-tag">
--
-- Runs on the AST, so code blocks and inline code are never touched.
-- Needs the reader extension +wikilinks_title_after_pipe, which turns
-- [[...]] into Link elements with class "wikilink".
--
-- The vault file list comes from server.py via env vars:
--   MD_INDEX  file of vault-relative paths, one per line, shortest first
--   MD_DOC    vault-relative path of the document being rendered

local IMAGE = { png=1, jpg=1, jpeg=1, gif=1, svg=1, webp=1, bmp=1, ico=1 }
local VIDEO = { mp4=1, webm=1, ogv=1, mov=1 }
local AUDIO = { mp3=1, ogg=1, wav=1, flac=1, m4a=1 }

local lower = pandoc.text.lower

-- ---------------------------------------------------------------------------
-- Vault index

local exists = {}     -- relpath -> true
local by_name = {}    -- lowercase basename -> { relpath, ... } (shortest first)
local doc_dir = ''

local function basename(p) return p:match('([^/]*)$') end
local function dirname(p) return p:match('^(.*)/[^/]*$') or '' end

do
  local f = io.open(os.getenv('MD_INDEX') or '', 'r')
  if f then
    for line in f:lines() do
      if line ~= '' then
        exists[line] = true
        local key = lower(basename(line))
        by_name[key] = by_name[key] or {}
        table.insert(by_name[key], line)
      end
    end
    f:close()
  end
  doc_dir = dirname(os.getenv('MD_DOC') or '')
end

-- Join and normalise a vault-relative path; nil if it climbs out of the vault.
local function join(dir, rel)
  local parts = {}
  for seg in ((dir ~= '' and dir .. '/' or '') .. rel):gmatch('[^/]+') do
    if seg == '..' then
      if #parts == 0 then return nil end
      table.remove(parts)
    elseif seg ~= '.' then
      table.insert(parts, seg)
    end
  end
  return table.concat(parts, '/')
end

-- Search order: same directory, exact-case basename anywhere,
-- case-insensitive fallback. [[folder/note]] matches on path suffix.
local function find(name)
  name = name:gsub('\\', '/')
  local same = join(doc_dir, name)
  if same and exists[same] then return same end

  local candidates = by_name[lower(basename(name))] or {}
  if name:find('/') then
    local suffix = '/' .. lower(name:gsub('^/+', ''))
    for _, p in ipairs(candidates) do
      local lp = '/' .. lower(p)
      if lp:sub(-#suffix) == suffix then return p end
    end
    return nil
  end
  local base = basename(name)
  for _, p in ipairs(candidates) do
    if basename(p) == base then return p end
  end
  return candidates[1]
end

-- Percent-encode like Python's urllib.parse.quote (safe = "/").
local function make_url(relpath)
  return '/' .. relpath:gsub('[^%w%-%._~/]', function(c)
    return string.format('%%%02X', c:byte())
  end)
end

-- Emulate pandoc's gfm_auto_identifiers: lowercase, drop punctuation,
-- spaces to hyphens. Non-ASCII letters are kept.
local function gfm_anchor(section)
  local s = lower((section:gsub('^%s+', ''):gsub('%s+$', '')))
  s = s:gsub('[^%w%-_ \128-\255]', '')
  return '#' .. s:gsub(' ', '-')
end

local function ext_of(p)
  return lower(p:match('%.([^./]+)$') or '')
end

local function missing(name, content)
  return pandoc.Span(content, pandoc.Attr('', { 'wiki-link-missing' },
    { title = 'Not found: ' .. name }))
end

-- ---------------------------------------------------------------------------
-- Wiki links and embeds

-- Pandoc's wikilink target is the raw text before the pipe; a pipe escaped
-- for a table cell outside a table leaves a trailing backslash.
local function target_of(link)
  return (link.target:gsub('\\$', ''))
end

local function resolve_link(link)
  local target = target_of(link)
  local name, section = target:match('^(.-)#(.*)$')
  if not name then name, section = target, nil end
  name = name:gsub('^%s+', ''):gsub('%s+$', '')

  -- Block references (#^id) have no rendered anchor
  local anchor = ''
  if section and section:sub(1, 1) ~= '^' then anchor = gfm_anchor(section) end

  if name == '' then
    return pandoc.Link(link.content, anchor)
  end

  local found = find(name .. '.md')
  if not found and name:find('%.') then found = find(name) end
  if found then
    return pandoc.Link(link.content, make_url(found) .. anchor)
  end
  return missing(name, link.content)
end

local function resolve_embed(link)
  local name = target_of(link):gsub('^%s+', ''):gsub('%s+$', '')
  local label = pandoc.utils.stringify(link.content)
  -- Without a pipe, pandoc sets the content to the target itself
  local alt = (label ~= name) and label or ''

  local found = find(name)
  if not found and ext_of(name) == '' then found = find(name .. '.md') end
  if not found then
    return missing(name, { pandoc.Str(name) })
  end

  local url = make_url(found)
  local ext = ext_of(found)
  local esc = function(s) return (s:gsub('&', '&amp;'):gsub('"', '&quot;'):gsub('<', '&lt;')) end

  if IMAGE[ext] then
    local w, h = alt:match('^(%d+)x(%d+)$')
    w = w or alt:match('^(%d+)$')
    if w then
      local style = 'width:' .. w .. 'px;' .. (h and ('height:' .. h .. 'px;') or '')
      return pandoc.RawInline('html', '<img src="' .. url .. '" alt="' .. esc(name)
        .. '" style="' .. style .. '">')
    end
    return pandoc.Image(alt ~= '' and alt or name, url)
  end
  if VIDEO[ext] then
    return pandoc.RawInline('html', '<video controls src="' .. url .. '"></video>')
  end
  if AUDIO[ext] then
    return pandoc.RawInline('html', '<audio controls src="' .. url .. '"></audio>')
  end
  if ext == 'pdf' then
    return pandoc.RawInline('html', '<iframe src="' .. url
      .. '" style="width:100%;height:600px;border:none;"></iframe>')
  end
  -- Non-media (including notes): link to it
  return pandoc.Link(alt ~= '' and alt or name, url)
end

local function is_wikilink(el)
  return el.t == 'Link' and el.classes:includes('wikilink')
end

-- An embed is a wikilink directly preceded by a Str ending in "!".
local function wikilinks(inlines)
  local out = pandoc.Inlines({})
  for i, el in ipairs(inlines) do
    if is_wikilink(el) then
      local prev = out[#out]
      if prev and prev.t == 'Str' and prev.text:sub(-1) == '!' then
        if #prev.text == 1 then
          out:remove(#out)
        else
          prev.text = prev.text:sub(1, -2)
        end
        out:insert(resolve_embed(el))
      else
        out:insert(resolve_link(el))
      end
    else
      out:insert(el)
    end
  end
  return out
end

-- ---------------------------------------------------------------------------
-- ==highlight==
--
-- Pandoc leaves "==" inside Str tokens: "==hi==", "==two" Space "words==",
-- "==" Strong "t==". Split each Str at runs of exactly two "=" into markers,
-- then pair an opening marker (not followed by a space) with the next
-- closing marker (not preceded by a space). Unpaired markers stay literal,
-- so "a == b" and "x === y" are left alone.

local MARK = {}

local function is_space(el)
  return el == nil or el == MARK or el.t == 'Space' or el.t == 'SoftBreak'
    or el.t == 'LineBreak'
end

local function split_markers(inlines)
  local toks, any = {}, false
  for _, el in ipairs(inlines) do
    if el.t == 'Str' and el.text:find('==', 1, true) then
      local s, pos = el.text, 1
      while pos <= #s do
        local a, b = s:find('=+', pos)
        if not a then
          table.insert(toks, pandoc.Str(s:sub(pos)))
          break
        end
        if a > pos then table.insert(toks, pandoc.Str(s:sub(pos, a - 1))) end
        if b - a == 1 then
          table.insert(toks, MARK)
          any = true
        else
          table.insert(toks, pandoc.Str(s:sub(a, b)))
        end
        pos = b + 1
      end
    else
      table.insert(toks, el)
    end
  end
  return toks, any
end

-- Colored highlights (Colorful Highlights plugin syntax): a leading
-- colored-circle emoji picks the color and is hidden, as in Obsidian's
-- reading view. "==🟢text==" -> <mark class="hl-green">text</mark>.
local HL_COLOR = {
  ['🔴'] = 'red', ['🟠'] = 'orange', ['🟡'] = 'yellow',
  ['🟢'] = 'green', ['🔵'] = 'blue', ['🟣'] = 'purple',
}

local function mark_classes(inner)
  local first = inner[1]
  if first and first.t == 'Str' then
    for emoji, color in pairs(HL_COLOR) do
      if first.text:sub(1, #emoji) == emoji then
        local rest = first.text:sub(#emoji + 1)
        if rest == '' then
          inner:remove(1)
          if inner[1] and inner[1].t == 'Space' then inner:remove(1) end
        else
          inner[1] = pandoc.Str(rest)
        end
        return { 'mark', 'hl-' .. color }
      end
    end
  end
  return { 'mark' }
end

local function highlight(inlines)
  local toks, any = split_markers(inlines)
  if not any then return nil end

  local out = pandoc.Inlines({})
  local i = 1
  while i <= #toks do
    local t = toks[i]
    if t == MARK then
      local close = nil
      if not is_space(toks[i + 1]) then
        for j = i + 2, #toks do
          if toks[j] == MARK then
            if not is_space(toks[j - 1]) then close = j end
            break
          end
        end
      end
      if close then
        local inner = pandoc.Inlines({})
        for k = i + 1, close - 1 do inner:insert(toks[k]) end
        out:insert(pandoc.Span(inner, pandoc.Attr('', mark_classes(inner))))
        i = close + 1
      else
        out:insert(pandoc.Str('=='))
        i = i + 1
      end
    else
      out:insert(t)
      i = i + 1
    end
  end
  return out
end

-- ---------------------------------------------------------------------------
-- Manual page breaks

local BREAK = '<div class="page-break"></div>'

local function para_break(el)
  if #el.content == 1 and el.content[1].t == 'Str' and el.content[1].text == '\\newpage' then
    return pandoc.RawBlock('html', BREAK)
  end
end

local function raw_break(el)
  if el.format == 'html' and el.text:match('^%s*<!%-%-%s*[Pp][Aa][Gg][Ee][Bb][Rr][Ee][Aa][Kk]%s*%-%->%s*$') then
    return pandoc.RawBlock('html', BREAK)
  end
end

-- ---------------------------------------------------------------------------
-- A frontmatter `title:` makes pandoc add its own <h1 class="title">; when the
-- note already has an H1 that duplicates it, so keep the title for <title> only.

local function drop_duplicate_title(doc)
  if not doc.meta.title then return nil end
  for _, block in ipairs(doc.blocks) do
    if block.t == 'Header' and block.level == 1 then
      doc.meta.pagetitle = doc.meta.pagetitle or doc.meta.title
      doc.meta.title = nil
      return doc
    end
  end
end

-- ---------------------------------------------------------------------------
-- Frontmatter `tags:` and `related:` as a strip under the H1 (or at the top
-- when there is none). Runs before the wikilink pass, so [[links]] in
-- `related:` are resolved like any other. Hidden in print unless the page
-- has <html class="print-props"> (toolbar checkbox or the .meta suffix).

local function meta_items(value)
  if value == nil then return {} end
  if pandoc.utils.type(value) == 'List' then return value end
  return { value }
end

local function as_inlines(item)
  local t = pandoc.utils.type(item)
  if t == 'Inlines' then return item end
  if t == 'Blocks' then return pandoc.utils.blocks_to_inlines(item) end
  return pandoc.Inlines({ pandoc.Str(pandoc.utils.stringify(item)) })
end

local function props_row(label, cls, items)
  local row = pandoc.Inlines({
    pandoc.Span({ pandoc.Str(label) }, pandoc.Attr('', { 'doc-props-label' })),
  })
  for _, item in ipairs(items) do
    row:insert(pandoc.Space())
    row:insert(pandoc.Span(item, pandoc.Attr('', { cls })))
  end
  return pandoc.Div({ pandoc.Plain(row) }, pandoc.Attr('', { 'doc-props-row' }))
end

local function doc_props(doc)
  local tags = {}
  for _, item in ipairs(meta_items(doc.meta.tags)) do
    -- "tags: a, b" and "tags: a b" are single strings in YAML
    for tag in pandoc.utils.stringify(item):gmatch('[^,%s]+') do
      tags[#tags + 1] = pandoc.Inlines({ pandoc.Str('#' .. tag:gsub('^#', '')) })
    end
  end
  local related = {}
  for _, item in ipairs(meta_items(doc.meta.related)) do
    local inl = as_inlines(item)
    if #inl > 0 then related[#related + 1] = inl end
  end
  if #tags == 0 and #related == 0 then return nil end

  local rows = {}
  if #tags > 0 then rows[#rows + 1] = props_row('Tags', 'doc-tag', tags) end
  if #related > 0 then rows[#rows + 1] = props_row('Related', 'doc-related', related) end
  local div = pandoc.Div(rows, pandoc.Attr('', { 'doc-props' }))

  local first = doc.blocks[1]
  local at = (first and first.t == 'Header' and first.level == 1) and 2 or 1
  doc.blocks:insert(at, div)
  return doc
end

-- ---------------------------------------------------------------------------
-- Inline #tags, styled like the chips in the tags strip. Obsidian's rules: a
-- word starting with "#", made of letters, digits, _ - /, with at least one
-- non-digit ("#123" is not a tag). Link text and the strip itself are skipped.

local function tag_split(text)
  if text:sub(1, 1) ~= '#' then return nil end
  local i = 2
  while i <= #text do
    local c = text:byte(i)
    -- ASCII tag characters, or any byte of a multi-byte (non-ASCII) letter
    if not (c >= 128 or text:sub(i, i):match('[%w_/%-]')) then break end
    i = i + 1
  end
  local tag = text:sub(2, i - 1)
  if tag == '' or not tag:match('[^%d]') then return nil end
  return tag, text:sub(i)
end

local function inline_tags(inlines)
  local out, any = pandoc.Inlines({}), false
  for _, el in ipairs(inlines) do
    local tag, rest
    if el.t == 'Str' then tag, rest = tag_split(el.text) end
    if tag then
      -- Two Strs, so the span's own text doesn't match again on the way down
      out:insert(pandoc.Span({ pandoc.Str('#'), pandoc.Str(tag) },
        pandoc.Attr('', { 'doc-tag', 'inline-tag' })))
      if rest ~= '' then out:insert(pandoc.Str(rest)) end
      any = true
    else
      out:insert(el)
    end
  end
  return any and out or nil
end

local function skip_children(el) return el, false end

return {
  { Pandoc = drop_duplicate_title },
  { Pandoc = doc_props },
  { Inlines = wikilinks },
  { Inlines = highlight, Para = para_break, Plain = para_break, RawBlock = raw_break },
  {
    traverse = 'topdown',
    Link = skip_children,
    Div = function(el) if el.classes:includes('doc-props') then return el, false end end,
    Inlines = inline_tags,
  },
}
