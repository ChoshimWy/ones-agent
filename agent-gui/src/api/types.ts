export interface WorkItem {
  id: string;
  type: "requirement" | "defect";
  title: string;
  projectId?: string;
  projectName?: string;
  assignee?: string;
  priority?: "low" | "medium" | "high";
  onesStatus?: string;
  status:
    | "PENDING"
    | "PARSING"
    | "PLANNING"
    | "WAITING_APPROVAL"
    | "CODING"
    | "TESTING"
    | "PUSHING"
    | "REPORTING"
    | "SUCCESS"
    | "FAILED";
  branch?: string;
  commitHash?: string;
  riskLevel?: "low" | "medium" | "high";
  requiresApproval?: boolean;
  onesId?: string;
  planJson?: Record<string, unknown>;
  mappingStatus?: "mapped" | "missing" | "pending";
  analysisStatus?: "not_started" | "running" | "blocked" | "complete";
  rootCause?: string;
  fixSuggestions?: string[];
  analysisMarkdown?: string;
  analysisEvidence?: DefectAnalysisEvidence[];
  suggestedBranchName?: string;
  baseBranch?: string;
  executionStatus?: "idle" | "ready" | "created" | "failed";
  executionBranch?: string;
  executionId?: string;
  executionRequestedAt?: string;
  createdAt: string;
  updatedAt: string;
}

export interface DefectAnalysisEvidence {
  id: string;
  label: string;
  summary: string;
  kind: "file" | "log" | "summary" | "markdown" | "trace" | "codebase" | "note";
  path?: string;
  snippet?: string;
  source?: string;
}

export interface DefectBranchRequest {
  branchName?: string;
  baseBranch?: string;
  reason?: string;
}

export interface DefectAnalysisResult {
  status: "pending" | "analyzing" | "analyzed" | "blocked" | "failed";
  summary: string;
  rootCause: string;
  fixSuggestions: string[];
  evidence: DefectAnalysisEvidence[];
  markdown: string;
  confidence: number;
  updatedAt: string;
}

export interface DefectExecutionResult {
  status: "idle" | "ready" | "creating" | "created" | "failed";
  requestType: "bugfix" | "development";
  repoUrl?: string;
  baseBranch?: string;
  branchName?: string;
  branchUrl?: string;
  message?: string;
  updatedAt: string;
}

export interface DefectRecord
  extends Omit<
    WorkItem,
    "type" | "onesId" | "projectId" | "projectName" | "mappingStatus" | "analysisStatus" | "executionStatus"
  > {
  type: "defect";
  onesId: string;
  projectId: string;
  projectName: string;
  mappingStatus: "mapped" | "partial" | "missing";
  analysisStatus: DefectAnalysisResult["status"];
  analysisSummary?: string;
  analysis?: DefectAnalysisResult;
  execution?: DefectExecutionResult;
  executionStatus?: DefectExecutionResult["status"];
  selectedRepo?: ProjectRepo;
  codebaseStatus?: "ready" | "blocked" | "missing";
}

export interface DefectListParams {
  projectId?: string;
  assignee?: string;
  status?: WorkItem["status"];
  analysisStatus?: DefectAnalysisResult["status"];
  mappingStatus?: DefectRecord["mappingStatus"];
  search?: string;
  page?: number;
  pageSize?: number;
}

export interface OnesIteration {
  id: string;
  name: string;
  key?: string;
  projectId: string;
  projectName?: string;
  statusName?: string;
  statusCategory?: string;
}

export interface OnesTeamMember {
  id: string;
  name: string;
}

export interface DefectExecutionRequest {
  requestType: "bugfix" | "development";
  branchName?: string;
  baseBranch?: string;
  notes?: string;
}

export interface SSEEvent {
  type: "TASK_UPDATE" | "APPROVAL_REQUEST" | "SYSTEM_ALERT";
  payload: { taskId: string; status: string; message?: string };
  timestamp: number;
}

export type TaskAction = "pause" | "resume" | "retry" | "cancel" | "approve" | "reject";

export interface TaskActionPayload {
  action: TaskAction;
  reason?: string;
}

export interface MetricsSummary {
  activeTasks: number;
  successRate: number;
  avgDurationSec: number;
  todayFailures: number;
  dailyThroughput: { date: string; count: number; success: number }[];
}

export interface LogEntry {
  id: string;
  timestamp: string;
  level: "debug" | "info" | "warning" | "error" | "critical";
  taskId?: string;
  stage?: string;
  message: string;
  traceId?: string;
  context?: Record<string, unknown>;
}

export interface AppConfig {
  ones: { baseUrl: string; email: string; password: string; teamId: string; projectId: string };
  git: { repoUrl: string; branch: string; authType: "https" | "ssh" };
  llm: { provider: string; model: string; baseUrl: string; apiKey: string };
  cicd: { platform: "github" | "gitlab" | "none"; token?: string };
  webhook: { secret?: string; enabled: boolean };
  // Email SMTP configuration
  email?: {
    smtpHost: string;
    smtpPort: number;
    smtpUser: string;
    smtpPassword: string;
    sender: string;
    useTls: boolean;
  };
}

// New Scheduled Task type definitions
export interface ScheduledTask {
  id: string;
  name: string;
  enabled: boolean;
  cronExpr: string;
  projectId: string;
  assigneeId?: string;
  assigneeName?: string;
  itemType: "all" | "defect" | "requirement";
  action: "plan" | "analyze";
  notifyEmails: string;
  notifyWechat: boolean;
  lastRunAt?: string;
  lastRunCount?: number;
  createdAt: string;
  updatedAt: string;
}

export interface ScheduledTaskRun {
  id: string;
  taskId: string;
  taskName?: string;
  taskCronExpr?: string;
  taskAction?: string;
  taskProjectId?: string;
  status: "running" | "success" | "partial" | "failed";
  itemCount: number;
  startedAt: string;
  finishedAt?: string;
  errorMessage?: string;
  createdAt: string;
}

export interface ScheduledTaskRunListParams {
  taskId?: string;
  status?: string;
  search?: string;
  page?: number;
  pageSize?: number;
}

export interface ScheduledTaskRunItem {
  id: string;
  runId: string;
  taskId: string;
  itemUuid: string;
  itemName: string;
  itemType: string;
  projectId: string;
  projectName: string;
  assignee: string;
  statusName: string;
  priorityName: string;
  action: "plan" | "analyze" | string;
  planSummary: string;
  planSteps: string[];
  riskLevel: string;
  branchName: string;
  requiresHumanApproval: boolean;
  analysisMarkdown: string;
  withCodebase: boolean;
  errorMessage: string;
  itemSnapshot: Record<string, unknown>;
  createdAt: string;
}

export interface AITriggerResponse {
  ok: boolean;
  message?: string;
  itemId?: string;
  triggeredAt?: string;
}

export interface ProjectRepo {
  projectId: string;
  projectName: string;
  repoUrl: string;
  branch: string;
  iterationId?: string;
  iterationName?: string;
  iterationKey?: string;
}

export interface User {
  id: string;
  name: string;
  role: "admin" | "dev" | "viewer";
  avatar?: string;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
}
