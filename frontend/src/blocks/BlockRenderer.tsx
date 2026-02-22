import CodeBlock from "../components/CodeBlock";
import ThinkingBlock from "./ThinkingBlock";

interface Props {
  type: string;
  content: string;
}

/**
 * Routes a mutagent: block type to its React renderer.
 * Falls back to a plain CodeBlock for unknown types.
 */
export default function BlockRenderer({ type, content }: Props) {
  switch (type) {
    case "thinking":
      return <ThinkingBlock content={content} />;
    case "code":
      return <CodeBlock code={content} lang="" />;
    case "status":
      return (
        <div className="status-block">
          <pre>{content}</pre>
        </div>
      );
    default:
      return <CodeBlock code={content} lang="" />;
  }
}
