type EventHandler = (data: Record<string, unknown>) => void;

/**
 * Auto-reconnecting WebSocket with exponential backoff.
 */
export class ReconnectingWebSocket {
  private ws: WebSocket | null = null;
  private url: string;
  private onMessage: EventHandler;
  private onOpen?: () => void;
  private onClose?: () => void;
  private tokenFn?: () => string | null;
  private retryCount = 0;
  private maxRetries = 10;
  private closed = false;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    url: string,
    onMessage: EventHandler,
    opts?: {
      onOpen?: () => void;
      onClose?: () => void;
      tokenFn?: () => string | null;
    },
  ) {
    this.url = url;
    this.onMessage = onMessage;
    this.onOpen = opts?.onOpen;
    this.onClose = opts?.onClose;
    this.tokenFn = opts?.tokenFn;
    this.connect();
  }

  private connect() {
    if (this.closed) return;

    // Append auth token as query param when available
    let connectUrl = this.url;
    if (this.tokenFn) {
      const token = this.tokenFn();
      if (token) {
        const sep = connectUrl.includes("?") ? "&" : "?";
        connectUrl = `${connectUrl}${sep}token=${encodeURIComponent(token)}`;
      }
    }

    this.ws = new WebSocket(connectUrl);

    this.ws.onopen = () => {
      this.retryCount = 0;
      this.onOpen?.();
    };

    this.ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data as string) as Record<string, unknown>;
        this.onMessage(data);
      } catch {
        // ignore parse errors
      }
    };

    this.ws.onclose = () => {
      this.onClose?.();
      if (!this.closed && this.retryCount < this.maxRetries) {
        const delay = Math.min(1000 * 2 ** this.retryCount, 30000);
        this.retryCount++;
        this.retryTimer = setTimeout(() => this.connect(), delay);
      }
    };

    this.ws.onerror = () => {
      this.ws?.close();
    };
  }

  send(data: Record<string, unknown>) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  }

  close() {
    this.closed = true;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.ws?.close();
  }
}
