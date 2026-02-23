/**
 * Shiki highlighter singleton — lazy-loaded, with a small set of
 * pre-bundled languages for instant highlighting of common code.
 */
import { createHighlighter, type Highlighter } from "shiki";

let instance: Highlighter | null = null;
let loading: Promise<Highlighter> | null = null;

const PRELOADED_LANGS = [
  "python",
  "javascript",
  "typescript",
  "json",
  "bash",
  "shell",
  "html",
  "css",
  "yaml",
  "toml",
  "markdown",
  "sql",
  "diff",
  "xml",
];

export function getHighlighter(): Promise<Highlighter> {
  if (instance) return Promise.resolve(instance);
  if (!loading) {
    loading = createHighlighter({
      themes: ["dark-plus"],
      langs: PRELOADED_LANGS,
    }).then((h) => {
      instance = h;
      return h;
    });
  }
  return loading;
}

/**
 * Synchronously highlight code if the highlighter is ready.
 * Returns `null` when the highlighter hasn't finished loading yet.
 */
export function highlightCode(code: string, lang: string): string | null {
  if (!instance) return null;
  try {
    const loaded = instance.getLoadedLanguages();
    const effectiveLang = loaded.includes(lang) ? lang : "text";
    return instance.codeToHtml(code, { lang: effectiveLang, theme: "dark-plus" });
  } catch {
    return null;
  }
}
