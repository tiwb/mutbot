import { useEffect, useState } from "react";
import Editor from "@monaco-editor/react";
import { readFile } from "../lib/api";

interface Props {
  filePath: string;
  workspaceId: string;
  language?: string;
}

export default function CodeEditorPanel({ filePath, workspaceId, language }: Props) {
  const [content, setContent] = useState<string | null>(null);
  const [detectedLang, setDetectedLang] = useState(language ?? "plaintext");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!filePath || !workspaceId) return;

    readFile(workspaceId, filePath).then((data: { content?: string; language?: string; error?: string }) => {
      if (data.error) {
        setError(data.error);
      } else {
        setContent(data.content ?? "");
        if (data.language) setDetectedLang(data.language);
      }
    }).catch((err: Error) => {
      setError(err.message);
    });
  }, [filePath, workspaceId]);

  if (!filePath) {
    return (
      <div className="code-editor-panel">
        <div className="empty-state"><p>No file selected.</p></div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="code-editor-panel">
        <div className="code-editor-toolbar">
          <span className="code-editor-path">{filePath}</span>
        </div>
        <div className="empty-state"><p style={{ color: "var(--accent)" }}>{error}</p></div>
      </div>
    );
  }

  return (
    <div className="code-editor-panel">
      <div className="code-editor-toolbar">
        <span className="code-editor-path">{filePath}</span>
        <span style={{ marginLeft: "auto", opacity: 0.5 }}>{detectedLang}</span>
      </div>
      <div className="code-editor-body">
        {content !== null && (
          <Editor
            height="100%"
            language={detectedLang}
            value={content}
            theme="vs-dark"
            options={{
              readOnly: true,
              minimap: { enabled: true },
              wordWrap: "on",
              scrollBeyondLastLine: false,
              fontSize: 13,
              fontFamily: '"SF Mono", "Cascadia Code", Consolas, monospace',
            }}
          />
        )}
      </div>
    </div>
  );
}
