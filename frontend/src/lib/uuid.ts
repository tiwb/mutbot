/**
 * crypto.randomUUID() polyfill for non-secure contexts.
 *
 * crypto.randomUUID() is only available in Secure Contexts (HTTPS or localhost).
 * When accessing MutBot via a LAN IP over plain HTTP (e.g. http://192.168.1.100:8741),
 * the browser does not consider it a secure context and crypto.randomUUID is undefined.
 *
 * This helper falls back to crypto.getRandomValues() which works in all contexts.
 */
export function uuid(): string {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  // Fallback: generate UUID v4 from getRandomValues
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8]! & 0x3f) | 0x80; // variant 10
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
