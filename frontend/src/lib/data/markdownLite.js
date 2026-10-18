/**
 * markdownLite.js — minimal, dependency-free Markdown-to-HTML renderer for
 * LLM-authored prose on the /admin/mcp page (Lab chat replies, saved
 * research thesis text). Not a full CommonMark implementation — covers
 * only the subset Claude's own replies actually use: headers, bold,
 * italic, inline code, fenced code blocks, bullet/numbered lists, and
 * paragraphs.
 *
 * Security: ALL input is HTML-escaped FIRST (escapeHtml), then markdown
 * syntax is layered on top of the already-escaped text. A literal
 * `<script>` (or any other tag) in the source text can never reach the
 * DOM as a real element — every markdown transform below only wraps
 * already-safe, already-escaped text in new tags it creates itself.
 */

/** @param {string} s @returns {string} */
export function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * @param {string} text
 * @returns {string} HTML string, safe to mount via {@html}
 */
export function renderMarkdownLite(text) {
  if (!text) return '';
  const escaped = escapeHtml(text);

  // Fenced code blocks first (```...```) — pulled out before any other
  // transform touches their contents, so nothing inside a code block is
  // ever re-interpreted as markdown.
  /** @type {string[]} */
  const codeBlocks = [];
  let withPlaceholders = escaped.replace(/```[a-zA-Z0-9_-]*\n?([\s\S]*?)```/g, (_m, body) => {
    const idx = codeBlocks.push(`<pre class="md-code-block"><code>${body}</code></pre>`) - 1;
    return `\u0000CODEBLOCK${idx}\u0000`;
  });

  const lines = withPlaceholders.split('\n');
  /** @type {string[]} */
  const out = [];
  /** @type {string[]} */
  let para = [];
  /** @type {{type: 'ul'|'ol', items: string[]} | null} */
  let list = null;

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${para.join('<br>')}</p>`);
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      const tag = list.type;
      out.push(`<${tag}>${list.items.map(i => `<li>${i}</li>`).join('')}</${tag}>`);
      list = null;
    }
  };
  const inline = (/** @type {string} */ s) => s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(?<![*\w])\*([^*]+)\*(?!\w)/g, '<em>$1</em>')
    .replace(/(?<!\w)_([^_]+)_(?!\w)/g, '<em>$1</em>');

  for (const raw of lines) {
    const line = raw.trimEnd();
    const header = /^(#{1,6})\s+(.*)$/.exec(line);
    const bullet = /^[-*]\s+(.*)$/.exec(line);
    const numbered = /^\d+\.\s+(.*)$/.exec(line);

    if (header) {
      flushPara(); flushList();
      const level = Math.min(6, header[1].length) + 2; // # -> h3, ## -> h4, ...
      out.push(`<h${level} class="md-heading">${inline(header[2])}</h${level}>`);
    } else if (bullet) {
      flushPara();
      if (!list || list.type !== 'ul') { flushList(); list = { type: 'ul', items: [] }; }
      list.items.push(inline(bullet[1]));
    } else if (numbered) {
      flushPara();
      if (!list || list.type !== 'ol') { flushList(); list = { type: 'ol', items: [] }; }
      list.items.push(inline(numbered[1]));
    } else if (line.trim() === '') {
      flushPara(); flushList();
    } else if (/^\u0000CODEBLOCK\d+\u0000$/.test(line.trim())) {
      flushPara(); flushList();
      out.push(line.trim());
    } else {
      flushList();
      para.push(inline(line));
    }
  }
  flushPara(); flushList();

  let html = out.join('\n');
  html = html.replace(/\u0000CODEBLOCK(\d+)\u0000/g, (_m, i) => codeBlocks[Number(i)] ?? '');
  return html;
}
