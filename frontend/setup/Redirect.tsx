import { useEffect } from "react";

export function Redirect({ url }: { url: string }) {
  useEffect(() => {
    window.location.href = url;
  }, [url]);
  return <div style={{ padding: 16, color: "#888" }}>Redirecting…</div>;
}
