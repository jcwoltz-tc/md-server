-- Lua filter: render GitHub *and* Obsidian callouts with consistent styling
-- and emoji icons.
--
-- Pandoc's gfm reader natively recognises only the 5 GitHub alert types
--   > [!NOTE]  [!TIP]  [!IMPORTANT]  [!WARNING]  [!CAUTION]
-- and turns them into:
--   <div class="note"><div class="title"><p>Note</p></div>...</div>
-- The Div() handler below remaps those.
--
-- Obsidian's extended callouts are NOT recognised by pandoc and arrive as
-- plain blockquotes, e.g.  > [!info]  > [!success]-  > [!bug] Custom title
-- The BlockQuote() handler below detects and converts those.
--
-- We remap everything to:
--   <div class="callout callout-<color>">
--     <p><span class="callout-title">ICON Label</span></p>
--     ...body...
--   </div>
-- where <color> is one of the existing style classes: note, tip, important,
-- warning, caution.  Each type keeps its own icon + label.

-- type name (lowercase) -> { label, icon, cls }
-- cls selects the CSS color class (.callout-<cls>); aliases share a cls.
local types = {
  -- blue
  note      = { label = "Note",      icon = "\u{2139}\u{FE0F}", cls = "note" },
  -- cyan
  info      = { label = "Info",      icon = "\u{2139}\u{FE0F}", cls = "info" },
  -- indigo
  todo      = { label = "Todo",      icon = "\u{1F532}",         cls = "todo" },
  -- teal
  abstract  = { label = "Abstract",  icon = "\u{1F4CB}",         cls = "abstract" },
  summary   = { label = "Summary",   icon = "\u{1F4CB}",         cls = "abstract" },
  tldr      = { label = "TL;DR",     icon = "\u{1F4CB}",         cls = "abstract" },
  -- gray
  quote     = { label = "Quote",     icon = "\u{1F4AC}",         cls = "quote" },
  cite      = { label = "Quote",     icon = "\u{1F4AC}",         cls = "quote" },
  -- green (tip)
  tip       = { label = "Tip",       icon = "\u{1F4A1}",         cls = "tip" },
  hint      = { label = "Hint",      icon = "\u{1F4A1}",         cls = "tip" },
  -- emerald (success)
  success   = { label = "Success",   icon = "\u{2705}",          cls = "success" },
  check     = { label = "Check",     icon = "\u{2705}",          cls = "success" },
  done      = { label = "Done",      icon = "\u{2705}",          cls = "success" },
  -- purple (important)
  important = { label = "Important", icon = "\u{1F4CC}",         cls = "important" },
  -- violet (example)
  example   = { label = "Example",   icon = "\u{1F4C4}",         cls = "example" },
  -- orange (warning)
  warning   = { label = "Warning",   icon = "\u{26A0}\u{FE0F}",  cls = "warning" },
  attention = { label = "Attention", icon = "\u{26A0}\u{FE0F}",  cls = "warning" },
  -- amber (question)
  question  = { label = "Question",  icon = "\u{2753}",          cls = "question" },
  help      = { label = "Help",      icon = "\u{2753}",          cls = "question" },
  faq       = { label = "FAQ",       icon = "\u{2753}",          cls = "question" },
  -- red (caution)
  caution   = { label = "Caution",   icon = "\u{1F534}",         cls = "caution" },
  -- orange-red (failure)
  failure   = { label = "Failure",   icon = "\u{274C}",          cls = "failure" },
  fail      = { label = "Fail",      icon = "\u{274C}",          cls = "failure" },
  missing   = { label = "Missing",   icon = "\u{274C}",          cls = "failure" },
  -- deep red (danger)
  danger    = { label = "Danger",    icon = "\u{1F534}",         cls = "danger" },
  error     = { label = "Error",     icon = "\u{1F534}",         cls = "danger" },
  -- pink (bug)
  bug       = { label = "Bug",       icon = "\u{1F41B}",         cls = "bug" },
}

local function build_callout(info, label, body_blocks)
  local blocks = {
    pandoc.Para({
      pandoc.Span(
        { pandoc.Str(info.icon .. " " .. label) },
        pandoc.Attr("", { "callout-title" })
      )
    })
  }
  for _, block in ipairs(body_blocks) do
    table.insert(blocks, block)
  end
  return pandoc.Div(blocks, pandoc.Attr("", { "callout", "callout-" .. info.cls }))
end

-- Native GFM alerts: <div class="note"> etc.
function Div(el)
  for _, cls in ipairs(el.classes) do
    local info = types[cls]
    if info then
      -- Copy body blocks, skipping pandoc's own .title div
      local body = {}
      for _, block in ipairs(el.content) do
        local is_title = false
        if block.t == "Div" then
          for _, c in ipairs(block.classes) do
            if c == "title" then is_title = true end
          end
        end
        if not is_title then
          table.insert(body, block)
        end
      end
      return build_callout(info, info.label, body)
    end
  end
end

-- Obsidian callouts that pandoc left as plain blockquotes.
function BlockQuote(el)
  local blocks = el.content
  if #blocks == 0 then return nil end

  local first = blocks[1]
  if first.t ~= "Para" and first.t ~= "Plain" then return nil end

  -- Split the first paragraph at its first line break: the head is the
  -- "[!type] optional title" line, the tail is any body on the same paragraph.
  local inlines = first.content
  local split_at = nil
  for i, inl in ipairs(inlines) do
    if inl.t == "LineBreak" or inl.t == "SoftBreak" then
      split_at = i
      break
    end
  end

  local head, tail = {}, {}
  if split_at then
    for i = 1, split_at - 1 do table.insert(head, inlines[i]) end
    for i = split_at + 1, #inlines do table.insert(tail, inlines[i]) end
  else
    head = inlines
  end

  -- Parse "[!type]" with optional +/- fold marker and optional custom title.
  local head_text = pandoc.utils.stringify(pandoc.Span(head))
  local kind, rest = head_text:match("^%s*%[!([%w]+)%][%+%-]?%s*(.-)%s*$")
  if not kind then return nil end
  local info = types[kind:lower()]
  if not info then return nil end

  local label = info.label
  if rest and #rest > 0 then
    label = rest
  end

  -- Body = leftover inlines from line 1 (if any) + remaining blocks.
  local body = {}
  if #tail > 0 then
    table.insert(body, pandoc.Para(tail))
  end
  for i = 2, #blocks do
    table.insert(body, blocks[i])
  end

  return build_callout(info, label, body)
end
