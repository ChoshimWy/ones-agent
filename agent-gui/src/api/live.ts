import { useQueryClient } from "@tanstack/react-query";
import { useSSE } from "@/hooks/useSSE";
import type { SSEEvent } from "@/api/types";

export function useLiveEvents() {
  const qc = useQueryClient();

  function handleEvent(event: SSEEvent) {
    switch (event.type) {
      case "TASK_UPDATE":
        qc.invalidateQueries({ queryKey: ["tasks"] });
        qc.invalidateQueries({ queryKey: ["task", event.payload.taskId] });
        qc.invalidateQueries({ queryKey: ["metrics"] });
        break;
      case "APPROVAL_REQUEST":
        qc.invalidateQueries({ queryKey: ["tasks"] });
        qc.invalidateQueries({ queryKey: ["task", event.payload.taskId] });
        break;
      case "SYSTEM_ALERT":
        qc.invalidateQueries({ queryKey: ["metrics"] });
        break;
    }
  }

  return useSSE({
    url: "/api/v1/stream/events",
    onEvent: handleEvent,
  });
}
