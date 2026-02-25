import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import CodeBlock from "./CodeBlock";
import BlockRenderer from "../blocks/BlockRenderer";

interface Props {
  content: string;
  onSessionLink?: (sessionId: string) => void;
}

const SESSION_LINK_PREFIX = "mutbot://session/";

/** 允许 mutbot:// 协议通过 URL 安全过滤。 */
function urlTransform(url: string): string {
  if (url.startsWith("mutbot://")) return url;
  return defaultUrlTransform(url);
}

export default function Markdown({ content, onSessionLink }: Props) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={urlTransform}
        components={{
          pre({ children }) {
            // Unwrap <pre> so our custom code component controls rendering
            return <>{children}</>;
          },
          code(props) {
            const { className, children } = props;
            const langMatch = /language-(.+)/.exec(className || "");
            const code = String(children).replace(/\n$/, "");

            if (langMatch) {
              const lang = langMatch[1]!;
              // Route mutagent: blocks to BlockRenderer
              if (lang.startsWith("mutagent:")) {
                const blockType = lang.slice("mutagent:".length);
                return <BlockRenderer type={blockType} content={code} />;
              }
              return <CodeBlock code={code} lang={lang} />;
            }

            // Multi-line code without language → code block
            if (code.includes("\n")) {
              return <CodeBlock code={code} lang="" />;
            }

            // Inline code
            return <code className="inline-code">{children}</code>;
          },
          // Intercept links: handle mutbot://session/ URLs
          a({ href, children }) {
            if (href?.startsWith(SESSION_LINK_PREFIX) && onSessionLink) {
              const sessionId = href.slice(SESSION_LINK_PREFIX.length);
              return (
                <a
                  href="#"
                  className="session-link"
                  onClick={(e) => {
                    e.preventDefault();
                    onSessionLink(sessionId);
                  }}
                >
                  {children}
                </a>
              );
            }
            return (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            );
          },
          // Style tables for GFM
          table({ children }) {
            return (
              <div className="md-table-wrapper">
                <table>{children}</table>
              </div>
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
