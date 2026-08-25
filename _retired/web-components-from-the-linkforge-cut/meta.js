import { useEffect, useState } from "react";
import { api } from "./api.js";

// Loads the engine's status-label map once (cached for the session) so the UI shows
// human labels ("Connected", "Replied 🎉") and never raw status codes.
let _cache = null;

export function useMeta() {
  const [meta, setMeta] = useState(_cache);
  useEffect(() => {
    if (_cache) return;
    api.meta().then((m) => { _cache = m; setMeta(m); }).catch(() => {});
  }, []);
  return meta;
}

export function label(meta, code) {
  return meta?.status_display?.[code] || (code ? code.replace(/_/g, " ") : "");
}
