import { useSyncExternalStore } from "react";

const MOBILE_QUERY = "(max-width: 767px)";

let mql: MediaQueryList | null = null;

function getMql(): MediaQueryList {
  if (!mql) mql = window.matchMedia(MOBILE_QUERY);
  return mql;
}

function subscribe(cb: () => void): () => void {
  const m = getMql();
  m.addEventListener("change", cb);
  return () => m.removeEventListener("change", cb);
}

function getSnapshot(): boolean {
  return getMql().matches;
}

/** Returns `true` when viewport width < 768px (mobile mode). */
export function useMobileDetect(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot);
}
