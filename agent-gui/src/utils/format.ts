const STATUS_MAP: Record<string, { label: string; variant: "default" | "secondary" | "destructive" | "outline"; icon: string }> = {
  PENDING: { label: "Pending", variant: "outline", icon: "⏳" },
  PARSING: { label: "Parsing", variant: "secondary", icon: "📄" },
  PLANNING: { label: "Planning", variant: "secondary", icon: "🧠" },
  WAITING_APPROVAL: { label: "Approval", variant: "outline", icon: "🛑" },
  CODING: { label: "Coding", variant: "default", icon: "💻" },
  TESTING: { label: "Testing", variant: "default", icon: "🧪" },
  PUSHING: { label: "Pushing", variant: "default", icon: "🚀" },
  REPORTING: { label: "Reporting", variant: "secondary", icon: "📊" },
  SUCCESS: { label: "Success", variant: "default", icon: "✅" },
  FAILED: { label: "Failed", variant: "destructive", icon: "❌" },
};

const LEVEL_MAP: Record<string, { label: string; variant: "default" | "secondary" | "destructive" | "outline" }> = {
  debug: { label: "Debug", variant: "outline" },
  info: { label: "Info", variant: "secondary" },
  warning: { label: "Warning", variant: "outline" },
  error: { label: "Error", variant: "destructive" },
  critical: { label: "Critical", variant: "destructive" },
};

const RISK_MAP: Record<string, { label: string; variant: "default" | "secondary" | "destructive" | "outline" }> = {
  low: { label: "Low", variant: "secondary" },
  medium: { label: "Medium", variant: "outline" },
  high: { label: "High", variant: "destructive" },
};

export function getStatusMeta(status: string) {
  return STATUS_MAP[status] || { label: status, variant: "outline" as const, icon: "❓" };
}

export function getLevelMeta(level: string) {
  return LEVEL_MAP[level] || { label: level, variant: "outline" as const };
}

export function getRiskMeta(risk: string) {
  return RISK_MAP[risk] || { label: risk, variant: "outline" as const };
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

export function formatRelativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 60000) return "just now";
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
  return new Date(iso).toLocaleDateString();
}

export function maskSecret(value: string, visible = 4): string {
  if (value.length <= visible) return "•".repeat(value.length);
  return value.slice(0, visible) + "•".repeat(value.length - visible);
}
