import { useCallback, useEffect, useState } from "react";
import { getHighlighter, highlightCode } from "../lib/shiki";

interface Props {
  code: string;
  lang: string;
}

export default function CodeBlock({ code, lang }: Props) {
  const [html, setHtml] = useState<string | null>(() => highlightCode(code, lang));
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    // Try synchronous path first (highlighter already loaded)
    const sync = highlightCode(code, lang);
    if (sync) {
      setHtml(sync);
      return;
    }
    // Wait for async load
    let cancelled = false;
    getHighlighter().then(() => {
      if (!cancelled) setHtml(highlightCode(code, lang));
    });
    return () => {
      cancelled = true;
    };
  }, [code, lang]);

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [code]);

  return (
    <div className="code-block">
      <div className="code-block-header">
        <span className="code-block-lang">{lang || "text"}</span>
        <button className="code-block-copy" onClick={handleCopy}>
          {copied ? "Copied!" : "Copy"}
        </button>
      </div>
      {html ? (
        <div
          className="code-block-body"
          dangerouslySetInnerHTML={{ __html: html }}
        />
      ) : (
        <pre className="code-block-body">
          <code>{code}</code>
        </pre>
      )}
    </div>
  );
}
