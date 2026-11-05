/**
 * BindDeckLite host UI i18n (WS-529).
 *
 * Two entry points:
 *   * the browser calls `i18n.load(lang)` once at boot, which fetches
 *     `/api/i18n` and fills the table;
 *   * anything that only needs the default table (the pure modules such as
 *     log_follow.js, and their Node tests) can call `t()` straight away —
 *     zh-CN is the shipped default and is loaded synchronously from
 *     host/locales when `require` exists.
 *
 * The page never renders English from this file: index.html keeps the Chinese
 * text in the DOM as first paint, then `apply()` swaps it out, so a slow
 * `/api/i18n` cannot flash empty labels.
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module && module.exports) module.exports = api;
  if (root) root.i18n = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  const DEFAULT_LANG = "zh-CN";

  /** zh-CN with {placeholder} substitution, no DOM, no fetch. */
  function makeT(table, lang) {
    return function t(code, params) {
      let text = table[code];
      if (typeof text !== "string") text = code;
      if (!params) return text;
      return text.replace(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g, (match, name) =>
        Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match
      );
    };
  }

  let lang = DEFAULT_LANG;
  let messages = {};
  // Node (unit tests import the pure modules directly) reads the shipped
  // table from disk; the browser gets it from /api/i18n.
  if (typeof require === "function") {
    try {
      messages = require("../locales/zh-CN.json");
    } catch (err) {
      messages = {};
    }
  }

  let t = makeT(messages, lang);

  function setMessages(table, tag) {
    if (table && typeof table === "object") {
      messages = table;
      lang = typeof tag === "string" && tag ? tag : lang;
      t = makeT(messages, lang);
      if (root && root.document) root.document.documentElement.lang = lang;
    }
    return t;
  }

  /** Fetch one locale from the server and make it current. */
  async function load(tag) {
    const url = tag ? `/api/i18n?lang=${encodeURIComponent(tag)}` : "/api/i18n";
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error(`i18n: /api/i18n returned ${res.status}`);
    const data = await res.json();
    setMessages(data && data.messages, data && data.lang);
    return lang;
  }

  // `data-i18n-html` is only used by the help page, whose paragraphs carry
  // inline <code> markers. The markup comes from our own locale table, never
  // from user input or a device line.
  const ATTRS = {
    "data-i18n-title": "title",
    "data-i18n-placeholder": "placeholder",
    "data-i18n-aria-label": "aria-label",
  };

  /**
   * Translate every marked node under `scope`.
   *
   * `data-i18n` sets textContent (the Chinese text already in the element is
   * the first-paint fallback, so nothing flashes empty). The attribute
   * variants exist because tooltips and placeholders are user-visible too.
   */
  function apply(scope) {
    const node = scope || (root && root.document);
    if (!node || !node.querySelectorAll) return;
    node.querySelectorAll("[data-i18n]").forEach((el) => {
      const key = el.getAttribute("data-i18n");
      if (key) el.textContent = t(key);
    });
    node.querySelectorAll("[data-i18n-html]").forEach((el) => {
      const key = el.getAttribute("data-i18n-html");
      if (key) el.innerHTML = t(key);
    });
    Object.keys(ATTRS).forEach((dataAttr) => {
      node.querySelectorAll(`[${dataAttr}]`).forEach((el) => {
        const key = el.getAttribute(dataAttr);
        if (key) el.setAttribute(ATTRS[dataAttr], t(key));
      });
    });
  }

  // Global shorthand: app.js / keys.js are plain scripts, not modules.
  if (root) {
    root.t = (code, params) => t(code, params);
  }

  return {
    DEFAULT_LANG,
    load,
    setMessages,
    apply,
    t: (code, params) => t(code, params),
    get lang() {
      return lang;
    },
    get messages() {
      return messages;
    },
  };
});
