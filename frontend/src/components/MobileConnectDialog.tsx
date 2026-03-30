import { useState } from "react";
import { QRCodeSVG } from "qrcode.react";

interface Props {
  url: string;
  via: string;
  local?: boolean;
  onClose: () => void;
}

export default function MobileConnectDialog({ url, via, local, onClose }: Props) {
  const defaultMode = window.location.hostname.endsWith("mutbot.ai") ? "via" : "direct";
  const [mode, setMode] = useState<"via" | "direct">(defaultMode);

  const qrValue = mode === "via" ? via : url;

  return (
    <div className="mobile-connect-overlay" onClick={onClose}>
      <div className="mobile-connect-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="mobile-connect-header">
          <span>Mobile Connect</span>
          <button className="mobile-connect-close" onClick={onClose}>&times;</button>
        </div>

        {local ? (
          <div className="mobile-connect-empty">
            <p>The server is only accessible locally. To allow mobile connections, configure an external listen address:</p>
            <div className="mobile-connect-code">
              <div><strong>Config:</strong> <code>~/.mutbot/config.json</code></div>
              <pre>{`{ "listen": ["0.0.0.0:8741"] }`}</pre>
              <div><strong>CLI:</strong></div>
              <pre>python -m mutbot --listen 0.0.0.0:8741</pre>
            </div>
          </div>
        ) : (
          <div className="mobile-connect-body mobile-connect-single">
            <QRCodeSVG value={qrValue} size={200} />
            <div className="mobile-connect-url">{qrValue}</div>
            <button
              className="mobile-connect-toggle"
              onClick={() => setMode((m) => (m === "via" ? "direct" : "via"))}
            >
              {mode === "via" ? "Direct" : "Via mutbot.ai"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
