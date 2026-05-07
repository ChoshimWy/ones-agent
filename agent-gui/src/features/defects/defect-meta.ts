import type { DefectAnalysisResult, DefectRecord, WorkItem } from "@/api/types";

type BadgeVariant = "default" | "secondary" | "outline" | "destructive";

export function getWorkflowMeta(status: WorkItem["status"]): { label: string; variant: BadgeVariant } {
  switch (status) {
    case "PENDING":
      return { label: "Pending", variant: "outline" };
    case "PARSING":
      return { label: "Parsing", variant: "secondary" };
    case "PLANNING":
      return { label: "Planning", variant: "secondary" };
    case "WAITING_APPROVAL":
      return { label: "Waiting approval", variant: "secondary" };
    case "CODING":
      return { label: "Coding", variant: "default" };
    case "TESTING":
      return { label: "Testing", variant: "default" };
    case "PUSHING":
      return { label: "Pushing", variant: "default" };
    case "REPORTING":
      return { label: "Reporting", variant: "default" };
    case "SUCCESS":
      return { label: "Success", variant: "secondary" };
    case "FAILED":
      return { label: "Failed", variant: "destructive" };
    default:
      return { label: status, variant: "outline" };
  }
}

export function getMappingMeta(status: DefectRecord["mappingStatus"]): { label: string; variant: BadgeVariant } {
  switch (status) {
    case "mapped":
      return { label: "Mapped", variant: "secondary" };
    case "partial":
      return { label: "Partial", variant: "outline" };
    case "missing":
      return { label: "Missing", variant: "destructive" };
    default:
      return { label: "Unknown", variant: "outline" };
  }
}

export function getAnalysisMeta(status: DefectAnalysisResult["status"] | undefined): { label: string; variant: BadgeVariant } {
  switch (status) {
    case "analyzed":
      return { label: "Analyzed", variant: "secondary" };
    case "analyzing":
      return { label: "Analyzing", variant: "default" };
    case "blocked":
      return { label: "Blocked", variant: "destructive" };
    case "failed":
      return { label: "Failed", variant: "destructive" };
    case "pending":
    default:
      return { label: "Pending", variant: "outline" };
  }
}

export function getExecutionMeta(status: NonNullable<DefectRecord["execution"]>["status"] | undefined) {
  switch (status) {
    case "created":
      return { label: "Branch created", variant: "secondary" as const };
    case "creating":
      return { label: "Creating branch", variant: "default" as const };
    case "ready":
      return { label: "Ready", variant: "outline" as const };
    case "failed":
      return { label: "Failed", variant: "destructive" as const };
    default:
      return { label: "Idle", variant: "outline" as const };
  }
}

export function formatMaybe(value?: string | number | null) {
  if (value === undefined || value === null || value === "") return "—";
  return String(value);
}
