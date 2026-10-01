-- Lua filter: Obsidian syntax that pandoc's gfm reader doesn't render.
--
--   [[note]] [[note|label]] [[note#heading]]   resolved against the vault
--   ![[image.png|300]] ![[clip.mp4]] ![[doc.pdf]]   embeds
--   ==highlight==                              <mark>
--   \newpage  /  <!-- pagebreak -->            manual page break
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
        out:insert(pandoc.Span(inner, pandoc.Attr('', { 'mark' })))
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

return {
  { Pandoc = drop_duplicate_title },
  { Inlines = wikilinks },
  { Inlines = highlight, Para = para_break, Plain = para_break, RawBlock = raw_break },
}
