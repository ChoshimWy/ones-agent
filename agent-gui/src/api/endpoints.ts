import { apiClient } from "./client";
import type {
  WorkItem,
  DefectListParams,
  DefectRecord,
  DefectExecutionRequest,
  MetricsSummary,
  LogEntry,
  AppConfig,
  User,
  PaginatedResponse,
  TaskActionPayload,
  ProjectRepo,
  ScheduledTask,
  ScheduledTaskRun,
  ScheduledTaskRunListParams,
  ScheduledTaskRunItem,
  OnesIteration,
  OnesTeamMember,
} from "./types";

export async function fetchTasks(params?: {
  status?: string;
  type?: string;
  projectId?: string;
  page?: number;
  pageSize?: number;
  search?: string;
}): Promise<PaginatedResponse<WorkItem>> {
  const { data } = await apiClient.get("/tasks", { params });
  return data;
}

export async function fetchTaskDetail(id: string): Promise<WorkItem> {
  const { data } = await apiClient.get(`/tasks/${id}`);
  return data;
}

export async function fetchDefects(params?: DefectListParams): Promise<PaginatedResponse<DefectRecord>> {
  const { data } = await apiClient.get("/defects", { params });
  return data;
}

export async function fetchDefectDetail(id: string): Promise<DefectRecord> {
  const { data } = await apiClient.get(`/defects/${id}`);
  return data;
}

export async function createDefectExecution(
  id: string,
  payload: DefectExecutionRequest
): Promise<DefectRecord> {
  const { data } = await apiClient.post(`/defects/${id}/execution`, payload);
  return data;
}

export async function executeTaskAction(
  id: string,
  payload: TaskActionPayload
): Promise<WorkItem> {
  const { data } = await apiClient.post(`/tasks/${id}/action`, payload);
  return data;
}

export async function createDefectBranch(
  id: string,
  payload: DefectExecutionRequest
): Promise<DefectRecord> {
  const { data } = await apiClient.post(`/defects/${id}/execution`, payload);
  return data;
}

export async function fetchMetrics(): Promise<MetricsSummary> {
  const { data } = await apiClient.get("/metrics/summary");
  return data;
}

export async function fetchLogs(params?: {
  level?: string;
  taskId?: string;
  traceId?: string;
  from?: string;
  to?: string;
  page?: number;
  pageSize?: number;
}): Promise<PaginatedResponse<LogEntry>> {
  const { data } = await apiClient.get("/logs", { params });
  return data;
}

export async function fetchConfig(): Promise<AppConfig> {
  const { data } = await apiClient.get("/config");
  return data;
}

export async function updateConfig(config: Partial<AppConfig>): Promise<AppConfig> {
  const { data } = await apiClient.put("/config", config);
  return data;
}

export async function testConnection(
  section: keyof AppConfig
): Promise<{ ok: boolean; message: string }> {
  const { data } = await apiClient.post(`/config/test/${section}`);
  return data;
}

export async function fetchCurrentUser(): Promise<User> {
  const { data } = await apiClient.get("/auth/me");
  return data;
}

export async function login(
  email: string,
  password: string
): Promise<{ token: string; user: User }> {
  const { data } = await apiClient.post("/auth/login", { email, password });
  return data;
}

export async function approveTask(
  id: string,
  approved: boolean,
  reason?: string
): Promise<WorkItem> {
  return executeTaskAction(id, {
    action: approved ? "approve" : "reject",
    reason,
  });
}

export async function fetchProjectRepos(projectId?: string): Promise<ProjectRepo[]> {
  const { data } = await apiClient.get("/project-repos", { params: projectId ? { project_id: projectId } : {} });
  return data;
}

export async function addProjectRepo(mapping: Omit<ProjectRepo, "projectName"> & { projectName?: string }): Promise<ProjectRepo> {
  const { data } = await apiClient.post("/project-repos", mapping);
  return data;
}

export async function removeProjectRepo(projectId: string, repoUrl: string): Promise<void> {
  await apiClient.delete("/project-repos", { data: { projectId, repoUrl } });
}

export async function fetchOnesProjects(): Promise<{ id: string; name: string }[]> {
  const { data } = await apiClient.get("/ones/projects");
  return data;
}

export async function fetchOnesProjectIterations(projectId: string): Promise<OnesIteration[]> {
  const { data } = await apiClient.get(`/ones/projects/${projectId}/iterations`);
  return data;
}

export async function fetchOnesTeamMembers(projectId?: string): Promise<OnesTeamMember[]> {
  const { data } = await apiClient.get("/ones/team-members", { params: projectId ? { projectId } : {} });
  return data;
}

// Scheduled Tasks
export async function fetchScheduledTasks(params?: {
  page?: number;
  pageSize?: number;
  enabled?: boolean;
  projectId?: string;
  itemType?: "all" | "defect" | "requirement";
}): Promise<ScheduledTask[]> {
  const { data } = await apiClient.get("/scheduled-tasks", { params });
  return data;
}

export async function createScheduledTask(payload: Omit<ScheduledTask, "id" | "createdAt" | "updatedAt">): Promise<ScheduledTask> {
  const { data } = await apiClient.post("/scheduled-tasks", payload as any);
  return data;
}

export async function updateScheduledTask(id: string, payload: Partial<ScheduledTask>): Promise<ScheduledTask> {
  const { data } = await apiClient.put(`/scheduled-tasks/${id}`, payload);
  return data;
}

export async function deleteScheduledTask(id: string): Promise<void> {
  await apiClient.delete(`/scheduled-tasks/${id}`);
}

export async function triggerScheduledTask(id: string): Promise<{ triggered: boolean; taskId: string; count: number }> {
  const { data } = await apiClient.post(`/scheduled-tasks/${id}/trigger`, undefined, {
    timeout: 0,
  });
  return data;
}

export async function fetchScheduledTaskRuns(taskId: string): Promise<ScheduledTaskRun[]> {
  const { data } = await apiClient.get(`/scheduled-tasks/${taskId}/runs`);
  return data;
}

export async function fetchAllScheduledTaskRuns(params?: ScheduledTaskRunListParams): Promise<PaginatedResponse<ScheduledTaskRun>> {
  const { data } = await apiClient.get("/scheduled-task-runs", { params });
  return data;
}

export async function fetchScheduledTaskRunItems(runId: string): Promise<ScheduledTaskRunItem[]> {
  const { data } = await apiClient.get(`/scheduled-task-runs/${runId}/items`);
  return data;
}

export async function aiTrigger(payload: {
  itemId: string;
  action: "plan" | "analyze";
  notifyEmails?: string;
  notifyWechat?: boolean;
}): Promise<Record<string, unknown>> {
  const { data } = await apiClient.post("/ai/trigger", payload);
  return data;
}
