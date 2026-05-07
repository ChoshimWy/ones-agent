import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  fetchTasks,
  fetchTaskDetail,
  executeTaskAction,
  fetchDefects,
  fetchDefectDetail,
  createDefectExecution,
  fetchMetrics,
  fetchLogs,
  fetchConfig,
  updateConfig,
  testConnection,
  fetchProjectRepos,
  addProjectRepo,
  removeProjectRepo,
  fetchOnesProjects,
  fetchOnesProjectIterations,
  fetchOnesTeamMembers,
  fetchScheduledTasks,
  createScheduledTask,
  updateScheduledTask,
  deleteScheduledTask,
  triggerScheduledTask,
  fetchScheduledTaskRuns,
  fetchAllScheduledTaskRuns,
  fetchScheduledTaskRunItems,
  aiTrigger,
} from "@/api/endpoints";
import type {
  TaskActionPayload,
  AppConfig,
  ProjectRepo,
  DefectExecutionRequest,
  DefectListParams,
  ScheduledTask,
  ScheduledTaskRunListParams,
} from "@/api/types";

export function useTasks(params?: {
  status?: string;
  type?: string;
  projectId?: string;
  page?: number;
  pageSize?: number;
  search?: string;
}) {
  return useQuery({
    queryKey: ["tasks", params],
    queryFn: () => fetchTasks(params),
  });
}

export function useDefects(params?: DefectListParams) {
  return useQuery({
    queryKey: ["defects", params],
    queryFn: () => fetchDefects(params),
  });
}

export function useDefectDetail(id: string) {
  return useQuery({
    queryKey: ["defect", id],
    queryFn: () => fetchDefectDetail(id),
    enabled: !!id,
  });
}

export function useCreateDefectBranch(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: DefectExecutionRequest) => createDefectExecution(id, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["defects"] });
      qc.invalidateQueries({ queryKey: ["defect", id] });
    },
  });
}

export function useTaskDetail(id: string) {
  return useQuery({
    queryKey: ["task", id],
    queryFn: () => fetchTaskDetail(id),
    enabled: !!id,
  });
}

export function useTaskAction(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: TaskActionPayload) => executeTaskAction(id, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["task", id] });
    },
  });
}

export function useMetrics() {
  return useQuery({
    queryKey: ["metrics"],
    queryFn: fetchMetrics,
    refetchInterval: 30_000,
  });
}

export function useLogs(params?: {
  level?: string;
  taskId?: string;
  traceId?: string;
  from?: string;
  to?: string;
  page?: number;
  pageSize?: number;
}) {
  return useQuery({
    queryKey: ["logs", params],
    queryFn: () => fetchLogs(params),
  });
}

export function useConfig() {
  return useQuery({
    queryKey: ["config"],
    queryFn: fetchConfig,
  });
}

export function useUpdateConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (config: Partial<AppConfig>) => updateConfig(config),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["config"] }),
  });
}

export function useTestConnection() {
  return useMutation({
    mutationFn: (section: keyof AppConfig) => testConnection(section),
  });
}

export function useProjectRepos(projectId?: string) {
  return useQuery({
    queryKey: ["project-repos", projectId],
    queryFn: () => fetchProjectRepos(projectId),
  });
}

export function useAddProjectRepo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (mapping: Omit<ProjectRepo, "projectName"> & { projectName?: string }) => addProjectRepo(mapping),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["project-repos"] }),
  });
}

export function useRemoveProjectRepo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, repoUrl }: { projectId: string; repoUrl: string }) => removeProjectRepo(projectId, repoUrl),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["project-repos"] }),
  });
}

export function useOnesProjects() {
  return useQuery({
    queryKey: ["ones-projects"],
    queryFn: fetchOnesProjects,
    retry: 1,
  });
}

export function useOnesProjectIterations(projectId?: string) {
  return useQuery({
    queryKey: ["ones-project-iterations", projectId],
    queryFn: () => fetchOnesProjectIterations(projectId || ""),
    retry: 1,
    enabled: !!projectId,
  });
}

export function useOnesTeamMembers(projectId?: string) {
  return useQuery({
    queryKey: ["ones-team-members", projectId],
    queryFn: () => fetchOnesTeamMembers(projectId),
    retry: 1,
    enabled: projectId !== undefined ? !!projectId : true,
  });
}

// Scheduled Tasks
export function useScheduledTasks(params?: {
  page?: number;
  pageSize?: number;
}) {
  return useQuery({
    queryKey: ["scheduled-tasks", params],
    queryFn: () => fetchScheduledTasks(params),
  });
}

export function useCreateScheduledTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: Omit<ScheduledTask, "id" | "createdAt" | "updatedAt">) => createScheduledTask(payload as any),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scheduled-tasks"] }),
  });
}

export function useUpdateScheduledTask(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: Partial<ScheduledTask>) => updateScheduledTask(id, payload),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scheduled-tasks"] }),
  });
}

export function useDeleteScheduledTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteScheduledTask(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scheduled-tasks"] }),
  });
}

export function useTriggerScheduledTask(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => triggerScheduledTask(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scheduled-tasks"] }),
  });
}

export function useScheduledTaskRuns(taskId: string) {
  return useQuery({
    queryKey: ["scheduled-task-runs", taskId],
    queryFn: () => fetchScheduledTaskRuns(taskId),
    enabled: !!taskId,
  });
}

export function useAllScheduledTaskRuns(params?: ScheduledTaskRunListParams) {
  return useQuery({
    queryKey: ["all-scheduled-task-runs", params],
    queryFn: () => fetchAllScheduledTaskRuns(params),
    enabled: !!params,
  });
}

export function useScheduledTaskRunItems(runId: string) {
  return useQuery({
    queryKey: ["scheduled-task-run-items", runId],
    queryFn: () => fetchScheduledTaskRunItems(runId),
    enabled: !!runId,
  });
}

// AI Trigger
export function useAiTrigger() {
  return useMutation({
    mutationFn: (payload: { itemId: string; action: "plan" | "analyze"; notifyEmails?: string; notifyWechat?: boolean }) => aiTrigger(payload),
  });
}
