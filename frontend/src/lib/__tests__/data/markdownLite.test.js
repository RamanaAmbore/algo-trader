import { describe, it, expect } from 'vitest';
import { escapeHtml, renderMarkdownLite } from '../../data/markdownLite.js';

describe('escapeHtml', () => {
  it('escapes the five HTML-significant characters', () => {
    expect(escapeHtml('<script>alert("x") & \'y\'</script>'))
      .toBe('&lt;script&gt;alert(&quot;x&quot;) &amp; &#39;y&#39;&lt;/script&gt;');
  });
  it('handles null/undefined as empty string', () => {
    expect(escapeHtml(null)).toBe('');
    expect(escapeHtml(undefined)).toBe('');
  });
});

describe('renderMarkdownLite — security', () => {
  it('a literal <script> tag in the source text never becomes a real element', () => {
    const html = renderMarkdownLite('<script>alert(1)</script>');
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script&gt;');
  });

  it('an <img onerror=...> injection attempt is neutralized', () => {
    const html = renderMarkdownLite('<img src=x onerror="alert(1)">');
    expect(html).not.toMatch(/<img[^&]/);
    expect(html).toContain('&lt;img');
  });

  it('markdown transforms only wrap already-escaped text in new safe tags', () => {
    // A bold span whose CONTENT contains a tag-like string — the tag
    // text itself must stay escaped even though **bold** syntax around
    // it is rendered.
    const html = renderMarkdownLite('**<b>not real bold</b>**');
    expect(html).toContain('<strong>');
    expect(html).not.toContain('<b>not real bold</b>');
    expect(html).toContain('&lt;b&gt;');
  });

  it('empty/null input returns empty string, not throwing', () => {
    expect(renderMarkdownLite('')).toBe('');
    expect(renderMarkdownLite(null)).toBe('');
    expect(renderMarkdownLite(undefined)).toBe('');
  });
});

describe('renderMarkdownLite — formatting', () => {
  it('renders **bold** as <strong>', () => {
    expect(renderMarkdownLite('a **bold** word')).toContain('a <strong>bold</strong> word');
  });

  it('renders *italic*/_italic_ as <em>', () => {
    expect(renderMarkdownLite('an *italic* word')).toContain('<em>italic</em>');
    expect(renderMarkdownLite('an _italic_ word')).toContain('<em>italic</em>');
  });

  it('renders inline `code` as <code>', () => {
    expect(renderMarkdownLite('call `get_positions()` now')).toContain('<code>get_positions()</code>');
  });

  it('renders a fenced code block as <pre><code>', () => {
    const html = renderMarkdownLite('```\nline1\nline2\n```');
    expect(html).toContain('<pre class="md-code-block"><code>');
    expect(html).toContain('line1\nline2');
  });

  it('renders a bullet list as <ul><li>', () => {
    const html = renderMarkdownLite('- first\n- second');
    expect(html).toContain('<ul>');
    expect(html).toContain('<li>first</li>');
    expect(html).toContain('<li>second</li>');
  });

  it('renders a numbered list as <ol><li>', () => {
    const html = renderMarkdownLite('1. first\n2. second');
    expect(html).toContain('<ol>');
    expect(html).toContain('<li>first</li>');
  });

  it('renders a # header as a heading with the md-heading class', () => {
    const html = renderMarkdownLite('# Title');
    expect(html).toMatch(/<h\d class="md-heading">Title<\/h\d>/);
  });

  it('separate paragraphs (blank-line separated) each get their own <p>', () => {
    const html = renderMarkdownLite('first para\n\nsecond para');
    expect(html).toContain('<p>first para</p>');
    expect(html).toContain('<p>second para</p>');
  });

  it('a single newline within one paragraph becomes <br>, not a new <p>', () => {
    const html = renderMarkdownLite('line one\nline two');
    expect(html).toBe('<p>line one<br>line two</p>');
  });

  it('a real-world Claude-style reply with mixed bold/bullets renders correctly end to end', () => {
    const src = [
      "I can't give you a risk/reward read yet.",
      '',
      '- **Database query:** needed approval, none granted.',
      '- **Nothing read:** no quantities, strikes or prices.',
    ].join('\n');
    const html = renderMarkdownLite(src);
    expect(html).toContain('<p>I can&#39;t give you a risk/reward read yet.</p>');
    expect(html).toContain('<strong>Database query:</strong>');
    expect(html).toContain('<strong>Nothing read:</strong>');
    expect(html).toContain('<ul>');
  });
});
