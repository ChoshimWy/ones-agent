import { useState, useEffect, useCallback, useRef } from "react";
import type { SSEEvent } from "@/api/types";

interface UseSSEOptions {
  url: string;
  onEvent?: (event: SSEEvent) => void;
  maxRetries?: number;
  enabled?: boolean;
}

export function useSSE({ url, onEvent, maxRetries = 5, enabled = true }: UseSSEOptions) {
  const [connected, setConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState<SSEEvent | null>(null);
  const retryCount = useRef(0);
  const retryTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const esRef = useRef<EventSource | null>(null);

  const connect = useCallback(() => {
    if (!enabled) return;

    const token = localStorage.getItem("auth_token");
    const esUrl = token ? `${url}?token=${encodeURIComponent(token)}` : url;
    const es = new EventSource(esUrl);
    esRef.current = es;

    es.onopen = () => {
      setConnected(true);
      retryCount.current = 0;
    };

    es.onmessage = (event) => {
      try {
        const parsed: SSEEvent = JSON.parse(event.data);
        setLastEvent(parsed);
        onEvent?.(parsed);
      } catch {
        // ignore malformed events
      }
    };

    es.onerror = () => {
      setConnected(false);
      es.close();
      esRef.current = null;

      if (retryCount.current < maxRetries) {
        const delay = Math.min(1000 * Math.pow(2, retryCount.current), 16000);
        retryCount.current += 1;
        retryTimer.current = setTimeout(connect, delay);
      }
    };
  }, [url, onEvent, maxRetries, enabled]);

  useEffect(() => {
    connect();
    return () => {
      esRef.current?.close();
      esRef.current = null;
      if (retryTimer.current) clearTimeout(retryTimer.current);
    };
  }, [connect]);

  return { connected, lastEvent };
}
