import { useState } from "react";
import { QRCodeSVG } from "qrcode.react";

interface Address {
  url: string;
  via: string;
}

interface Props {
  addresses: Address[];
  onClose: () => void;
}

export default function MobileConnectDialog({ addresses, onClose }: Props) {
  // Default mode based on whether we're currently on mutbot.ai
  const defaultMode = window.location.hostname.endsWith("mutbot.ai") ? "via" : "direct";

  // Track which URL mode each address card uses
  const [modes, setModes] = useState<Record<number, "via" | "direct">>({});

  const toggleMode = (idx: number) => {
    setModes((prev) => ({
      ...prev,
      [idx]: (prev[idx] || defaultMode) === "direct" ? "via" : "direct",
    }));
  };

  const hasAddresses = addresses.length > 0;

  return (
    <div className="mobile-connect-overlay" onClick={onClose}>
      <div className="mobile-connect-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="mobile-connect-header">
          <span>Mobile Connect</span>
          <button className="mobile-connect-close" onClick={onClose}>&times;</button>
        </div>

        {hasAddresses ? (
          <div className="mobile-connect-body">
            {addresses.map((addr, idx) => {
              const mode = modes[idx] || defaultMode;
              const qrValue = mode === "via" ? addr.via : addr.url;
              return (
                <div key={idx} className="mobile-connect-card">
                  <QRCodeSVG value={qrValue} size={120} />
                  <div className="mobile-connect-info">
                    <div className="mobile-connect-url">{qrValue}</div>
                    <button
                      className="mobile-connect-toggle"
                      onClick={() => toggleMode(idx)}
                    >
                      {mode === "via" ? "Direct" : "Via mutbot.ai"}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="mobile-connect-empty">
            <p>No external addresses available.</p>
            <p>The server is listening on localhost only. To allow mobile connections, configure an external listen address:</p>
            <div className="mobile-connect-code">
              <div><strong>Config:</strong> <code>~/.mutbot/config.json</code></div>
              <pre>{`{ "listen": ["0.0.0.0:8741"] }`}</pre>
              <div><strong>CLI:</strong></div>
              <pre>python -m mutbot --listen 0.0.0.0:8741</pre>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
