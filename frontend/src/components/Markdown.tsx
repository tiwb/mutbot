import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import CodeBlock from "./CodeBlock";
import BlockRenderer from "../blocks/BlockRenderer";

interface Props {
  content: string;
}

export default function Markdown({ content }: Props) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
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
