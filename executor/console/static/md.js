/* SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0 */
/* Minimal Markdown → safe HTML for chat bubbles.
   Supports: *bold*, **bold**, `code`, images, links, autolinks, line breaks.
   Escapes everything else — the only tags emitted are the ones this function
   builds itself, always from URL-sanitized, entity-escaped inputs. */

// Only http(s), mailto and tel URLs may become href/src — everything else
// (javascript:, data:, vbscript:, …) is dropped so an attacker can't smuggle
// script through a link or image.
function _mdSafeUrl(u) {
  const url = String(u).trim();
  if (/^(https?:\/\/|mailto:|tel:)/i.test(url)) return url;
  return "";
}

window.renderMd = function (s) {
  if (s == null) return "";
  // Escape FIRST — nothing below re-introduces a raw '<' or '>' except the
  // tags we build ourselves. Quotes stay as entities so they can't break out
  // of an attribute.
  const esc = String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  // Backtick code (already escaped above)
  let out = esc.replace(/`([^`\n]+)`/g, (_, body) => `<code>${body}</code>`);

  // Images: ![alt](url) — render inline, url-sanitized. Must run before links.
  out = out.replace(/!\[([^\]\n]*)\]\(([^)\s]+)\)/g, (m, alt, url) => {
    const safe = _mdSafeUrl(url);
    if (!safe) return m;
    return `<img class="md-img" src="${safe}" alt="${alt}" loading="lazy">`;
  });

  // Links: [text](url) — open in a new tab, url-sanitized.
  out = out.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, txt, url) => {
    const safe = _mdSafeUrl(url);
    if (!safe) return m;
    return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${txt}</a>`;
  });

  // Bare autolinks: http(s):// URLs at a word boundary that are NOT already
  // inside an attribute (i.e. not preceded by =" from a link/img built above).
  out = out.replace(/(^|[\s(>])(https?:\/\/[^\s<"]+)/g, (m, pre, url) => {
    const safe = _mdSafeUrl(url);
    if (!safe) return m;
    return `${pre}<a href="${safe}" target="_blank" rel="noopener noreferrer">${url}</a>`;
  });

  // **bold** first (greedy match prevention via non-greedy)
  out = out.replace(/\*\*([^*\n]+?)\*\*/g, "<strong>$1</strong>");
  // *bold* (single-asterisk, matches Telegram's MarkdownV1 style)
  out = out.replace(/(^|[^*\w])\*([^*\n]+?)\*(?!\w)/g, "$1<strong>$2</strong>");

  // newlines → <br>
  out = out.replace(/\n/g, "<br>");

  return out;
};
