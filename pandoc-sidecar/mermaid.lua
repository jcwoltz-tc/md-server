-- Lua filter: pass ```mermaid code blocks through as <pre class="mermaid">
-- so mermaid.js (loaded by mermaid.html) can render them client-side.

function CodeBlock(el)
  if el.classes:includes('mermaid') then
    local escaped = el.text
      :gsub('&', '&amp;')
      :gsub('<', '&lt;')
      :gsub('>', '&gt;')
    return pandoc.RawBlock('html', '<pre class="mermaid">' .. escaped .. '</pre>')
  end
end
