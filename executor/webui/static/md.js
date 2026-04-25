/* SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0 */
/* Minimal Markdown → safe HTML for chat bubbles.
   Supports: *bold*, **bold**, `code`, line breaks. Escapes everything else. */

window.renderMd = function (s) {
  if (s == null) return "";
  const esc = String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  // Backtick code (escape inside, then wrap)
  let out = esc.replace(/`([^`\n]+)`/g, (_, body) => `<code>${body}</code>`);

  // **bold** first (greedy match prevention via non-greedy)
  out = out.replace(/\*\*([^*\n]+?)\*\*/g, "<strong>$1</strong>");
  // *bold* (single-asterisk, matches Telegram's MarkdownV1 style)
  out = out.replace(/(^|[^*\w])\*([^*\n]+?)\*(?!\w)/g, "$1<strong>$2</strong>");

  // newlines → <br>
  out = out.replace(/\n/g, "<br>");

  return out;
};
