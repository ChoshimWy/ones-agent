import { http, HttpResponse, delay } from "msw";
import type { WorkItem, MetricsSummary, LogEntry, AppConfig, ProjectRepo, ScheduledTask, TaskActionPayload, ScheduledTaskRun, ScheduledTaskRunItem, DefectRecord, DefectExecutionRequest } from "@/api/types";

type ScheduledTaskCreatePayload = Partial<Omit<ScheduledTask, "id" | "createdAt" | "updatedAt">> & {
  name?: string;
};

const ACTION_STATUS: Record<TaskActionPayload["action"], WorkItem["status"]> = {
  approve: "CODING",
  reject: "FAILED",
  pause: "WAITING_APPROVAL",
  resume: "CODING",
  retry: "PENDING",
  cancel: "FAILED",
};

const ITEMS: WorkItem[] = Array.from({ length: 42 }, (_, i) => ({
  id: `task-${i + 1}`,
  type: i % 3 === 0 ? "defect" : "requirement",
  title: `${i % 3 === 0 ? "Fix" : "Implement"} feature ${i + 1}`,
  projectId: ["proj-001", "proj-002", "proj-003", "proj-004"][i % 4],
  projectName: ["Core Platform", "Mobile App", "Data Pipeline", "Internal Tools"][i % 4],
  assignee: ["Alice", "Bob", "Carol", "Dylan"][i % 4],
  priority: (["low", "medium", "high"] as const)[i % 3],
  onesStatus: ["Open", "In Progress", "Resolved", "Reopened"][i % 4],
  status: (["PENDING", "PLANNING", "CODING", "TESTING", "WAITING_APPROVAL", "SUCCESS", "FAILED"] as const)[i % 7],
  branch: i % 2 === 0 ? `feat/REQ-${i + 1}-feature` : undefined,
  commitHash: i % 3 === 0 ? `abc${i}def` : undefined,
  riskLevel: (["low", "medium", "high"] as const)[i % 3],
  requiresApproval: i % 5 === 0,
  onesId: `ONES-${1000 + i}`,
  mappingStatus: i % 3 === 0 ? "mapped" : i % 4 === 0 ? "pending" : "missing",
  analysisStatus: i % 3 === 0 ? "complete" : i % 4 === 0 ? "running" : "blocked",
  rootCause: i % 3 === 0 ? "Event handler state is not updated after the defect reproduces." : undefined,
  fixSuggestions: i % 3 === 0 ? ["Trace the defect to the shared handler boundary.", "Add a regression test for the affected flow."] : undefined,
  analysisMarkdown: i % 3 === 0 ? `### Root cause\n\nThe defect is reproducible in the ${["Core Platform", "Mobile App", "Data Pipeline", "Internal Tools"][i % 4]} code path.\n\n- Evidence points to a stale state transition.\n- The current branch does not include the fix.\n\n**Suggested action:** create a repair branch and validate the affected path.` : undefined,
  analysisEvidence: i % 3 === 0 ? [
    { id: `ev-${i}-1`, label: "Source file excerpt", summary: "The defect points to a source file boundary that misses a state update.", kind: "file", path: `src/features/feature-${i + 1}.tsx`, snippet: "Missing state update after async completion.", source: "Codebase scan" },
    { id: `ev-${i}-2`, label: "Planner note", summary: "The workflow should reproduce, isolate, patch, and verify the defect path.", kind: "summary", snippet: "Reproduce, isolate, patch, and verify the affected defect path.", source: "Analysis service" },
  ] : undefined,
  suggestedBranchName: i % 3 === 0 ? `fix/ones-${1000 + i}-feature-${i + 1}` : undefined,
  baseBranch: ["main", "develop", "release", "main"][i % 4],
  executionStatus: i % 3 === 0 ? "ready" : "idle",
  executionBranch: i % 5 === 0 ? `fix/ones-${1000 + i}-feature-${i + 1}` : undefined,
  executionId: i % 5 === 0 ? `exec-${i + 1}` : undefined,
  executionRequestedAt: i % 5 === 0 ? new Date(Date.now() - i * 1800000).toISOString() : undefined,
  createdAt: new Date(Date.now() - i * 3600000).toISOString(),
  updatedAt: new Date(Date.now() - i * 1800000).toISOString(),
}));

const METRICS: MetricsSummary = {
  activeTasks: 12,
  successRate: 0.87,
  avgDurationSec: 342,
  todayFailures: 2,
  dailyThroughput: Array.from({ length: 7 }, (_, i) => ({
    date: new Date(Date.now() - (6 - i) * 86400000).toISOString().slice(0, 10),
    count: 15 + Math.floor(Math.random() * 10),
    success: 12 + Math.floor(Math.random() * 8),
  })),
};

const LOGS: LogEntry[] = Array.from({ length: 50 }, (_, i) => ({
  id: `log-${i + 1}`,
  timestamp: new Date(Date.now() - i * 120000).toISOString(),
  level: (["info", "info", "info", "warning", "error", "debug"] as const)[i % 6],
  taskId: i % 3 === 0 ? `task-${(i % 42) + 1}` : undefined,
  stage: (["PLANNING", "CODING", "TESTING", "PUSHING"] as const)[i % 4],
  message: `Log message ${i + 1}: operation completed`,
  traceId: i % 4 === 0 ? `trace-${i}` : undefined,
  context: i % 4 === 0 ? { key: `value-${i}` } : undefined,
}));

const CONFIG: AppConfig = {
  ones: {
    baseUrl: "https://ones.example.com",
    email: "dev@example.com",
    password: "••••••••",
    teamId: "team-001",
    projectId: "proj-001",
  },
  git: { repoUrl: "https://github.com/example/repo", branch: "main", authType: "https" },
  llm: { provider: "openai", model: "gpt-4", baseUrl: "https://api.openai.com/v1", apiKey: "••••••••" },
  cicd: { platform: "github", token: "ghp_xxxxxxxxxxxx" },
  webhook: { secret: "whsec_xxxxxxxxxxxx", enabled: true },
  email: { smtpHost: "smtp.example.com", smtpPort: 465, smtpUser: "bot@example.com", smtpPassword: "••••••••", sender: "bot@example.com", useTls: true },
};

const ONES_PROJECTS = [
  { id: "proj-001", name: "Core Platform" },
  { id: "proj-002", name: "Mobile App" },
  { id: "proj-003", name: "Data Pipeline" },
  { id: "proj-004", name: "Internal Tools" },
];

const ONES_TEAM_MEMBERS = [
  { id: "user-001", name: "Alice" },
  { id: "user-002", name: "Bob" },
  { id: "user-003", name: "Carol" },
  { id: "user-004", name: "Dylan" },
];

const ONES_ITERATIONS: Record<string, { id: string; name: string; key: string; projectId: string; projectName: string; statusName: string; statusCategory: string }[]> = {
  "proj-001": [
    { id: "sprint-001-a", name: "Core Platform Sprint 24.10", key: "CP-24.10", projectId: "proj-001", projectName: "Core Platform", statusName: "进行中", statusCategory: "open" },
    { id: "sprint-001-b", name: "Core Platform Sprint 24.11", key: "CP-24.11", projectId: "proj-001", projectName: "Core Platform", statusName: "规划中", statusCategory: "pending" },
  ],
  "proj-002": [
    { id: "sprint-002-a", name: "Mobile App Sprint 18", key: "MA-18", projectId: "proj-002", projectName: "Mobile App", statusName: "进行中", statusCategory: "open" },
  ],
  "proj-003": [
    { id: "sprint-003-a", name: "Data Pipeline Iteration Alpha", key: "DP-A", projectId: "proj-003", projectName: "Data Pipeline", statusName: "进行中", statusCategory: "open" },
  ],
  "proj-004": [
    { id: "sprint-004-a", name: "Internal Tools Iteration 7", key: "IT-7", projectId: "proj-004", projectName: "Internal Tools", statusName: "规划中", statusCategory: "pending" },
  ],
};

const DEFECT_STORIES = [
  {
    title: "Login button stays disabled after validation",
    summary: "The submit button never re-enables once async validation resolves, so the defect feels like a dead end.",
    rootCause: "The form clears its pending state in the success branch only, leaving one code path stuck in a loading state.",
    fixes: ["Move the loading reset into a finally branch.", "Add a regression test for the post-validation enable state."],
    evidence: [
      {
        id: "login-form-state",
        label: "Login form state guard",
        kind: "file" as const,
        source: "src/features/auth/LoginForm.tsx",
        summary: "The disabled flag is derived from a loading state that is only cleared on the success path.",
        filePath: "src/features/auth/LoginForm.tsx",
        snippet: "if (result.ok) { setSubmitting(false); return; }",
      },
      {
        id: "login-qa-note",
        label: "QA reproduction note",
        kind: "note" as const,
        source: "analysis",
        summary: "QA reproduced the issue after a slow network response and found the button remained greyed out.",
      },
    ],
  },
  {
    title: "Export workflow times out on large projects",
    summary: "Exports stall when the project has enough records to exceed the current search window.",
    rootCause: "The export job reuses the interactive page query instead of a paginated backend export path.",
    fixes: ["Switch export to a dedicated paginated endpoint.", "Show a progress state while the export batches records."],
    evidence: [
      {
        id: "export-service-limit",
        label: "Export service limit",
        kind: "codebase" as const,
        source: "src/services/export.ts",
        summary: "The export service reuses the same limit as the table view and stops early.",
        filePath: "src/services/export.ts",
        snippet: "const rows = await fetchRows({ pageSize: 20 });",
      },
      {
        id: "export-timeout-trace",
        label: "Timeout trace",
        kind: "trace" as const,
        source: "runtime",
        summary: "Trace logs show the request timing out after the second page fetch.",
      },
    ],
  },
  {
    title: "Attachment preview fails for converted images",
    summary: "Converted image attachments load in the list but fail when the preview panel opens.",
    rootCause: "Preview normalization strips the converted MIME type and the image viewer rejects the fallback payload.",
    fixes: ["Preserve the converted MIME type in the preview payload.", "Add a fallback for non-native image formats."],
    evidence: [
      {
        id: "attachment-preview-guard",
        label: "Preview guard",
        kind: "file" as const,
        source: "src/components/AttachmentPreview.tsx",
        summary: "The preview component only accepts native image MIME types.",
        filePath: "src/components/AttachmentPreview.tsx",
        snippet: "if (!mime.startsWith('image/')) return null;",
      },
    ],
  },
  {
    title: "Project filter resets after analysis refresh",
    summary: "Changing analysis state refreshes the page and clears the selected project filter.",
    rootCause: "The refresh handler rebuilds query params from scratch instead of preserving the current project scope.",
    fixes: ["Persist the active project filter through refreshes.", "Use URL search params as the single source of truth."],
    evidence: [
      {
        id: "filter-reopen-note",
        label: "QA reproduction note",
        kind: "note" as const,
        source: "QA",
        summary: "Users reported the defect after switching from one ONES project to another mid-review.",
      },
      {
        id: "filter-refresh-handler",
        label: "Refresh handler",
        kind: "codebase" as const,
        source: "src/routes/defects.tsx",
        summary: "The defect list rebuilds search params on refresh without merging the current project selection.",
      },
    ],
  },
  {
    title: "Branch creation misses the resolved repo branch",
    summary: "Branch creation can target the wrong base branch when the repo mapping has a branch override.",
    rootCause: "The execution step falls back to the default branch instead of the repo resolver output.",
    fixes: ["Carry the resolved branch into the execution request.", "Render the selected repository and branch before branch creation."],
    evidence: [
      {
        id: "execution-resolver-summary",
        label: "Resolver summary",
        kind: "summary" as const,
        source: "analysis",
        summary: "The defect already has a mapped repo, but the current execution UI hides the selected branch.",
      },
    ],
  },
];

const DEFECTS: DefectRecord[] = ITEMS.filter((item) => item.type === "defect").map((item, index) => {
  const project = ONES_PROJECTS[index % ONES_PROJECTS.length];
  const story = DEFECT_STORIES[index % DEFECT_STORIES.length];
  const mapped = index % 5 !== 4;
  const analyzed = index % 4 !== 3;
  const analysisStatus: DefectRecord["analysisStatus"] = analyzed ? "analyzed" : index % 2 === 0 ? "blocked" : "pending";
  const mappingStatus: DefectRecord["mappingStatus"] = mapped ? (index % 3 === 0 ? "partial" : "mapped") : "missing";
  const branchName = `${mapped ? "fix" : "investigate"}/${(item.onesId || `ONES-${2000 + index}`).toLowerCase()}`;
  const analysisMarkdown = [
    `# ${story.title}`,
    "",
    `## Root cause`,
    story.rootCause,
    "",
    `## Fix suggestions`,
    ...story.fixes.map((fix) => `- ${fix}`),
    "",
    `## Evidence`,
    ...story.evidence.map((evidence) => `- ${evidence.summary}`),
  ].join("\n");

  const analysis = {
    status: analysisStatus,
    summary: story.summary,
    rootCause: story.rootCause,
    fixSuggestions: story.fixes,
    evidence: story.evidence,
    markdown: analysisMarkdown,
    confidence: analyzed ? (mappingStatus === "mapped" ? 0.88 : 0.72) : 0.38,
    updatedAt: new Date(Date.now() - index * 3600000).toISOString(),
  };

  return {
    ...item,
    type: "defect",
    onesId: item.onesId || `ONES-${2000 + index}`,
    title: story.title,
    projectId: project.id,
    projectName: project.name,
    mappingStatus,
    analysisStatus,
    analysisSummary: story.summary,
    analysis,
    analysisEvidence: story.evidence,
    rootCause: story.rootCause,
    fixSuggestions: story.fixes,
    analysisMarkdown,
    suggestedBranchName: branchName,
    baseBranch: project.id === "proj-002" ? "develop" : "main",
    executionStatus: analyzed && mappingStatus !== "missing" ? "ready" : "idle",
    executionBranch: analyzed && mappingStatus !== "missing" ? branchName : undefined,
    executionRequestedAt: analyzed && mappingStatus !== "missing" ? new Date(Date.now() - index * 1800000).toISOString() : undefined,
    selectedRepo: mapped
      ? {
          projectId: project.id,
          projectName: project.name,
          repoUrl: `https://github.com/example/${project.name.toLowerCase().replace(/\s+/g, "-")}.git`,
          branch: project.id === "proj-002" ? "develop" : "main",
          iterationId: ONES_ITERATIONS[project.id]?.[0]?.id,
          iterationName: ONES_ITERATIONS[project.id]?.[0]?.name,
          iterationKey: ONES_ITERATIONS[project.id]?.[0]?.key,
        }
      : undefined,
    codebaseStatus: mapped ? "ready" : "missing",
    execution: analyzed && mappingStatus !== "missing"
      ? {
          status: "ready",
          requestType: "bugfix",
          repoUrl: `https://github.com/example/${project.name.toLowerCase().replace(/\s+/g, "-")}.git`,
          baseBranch: project.id === "proj-002" ? "develop" : "main",
          branchName,
          updatedAt: new Date(Date.now() - index * 1800000).toISOString(),
        }
      : {
          status: "idle",
          requestType: "bugfix",
          baseBranch: project.id === "proj-002" ? "develop" : "main",
          updatedAt: new Date(Date.now() - index * 1800000).toISOString(),
        },
  } satisfies DefectRecord;
});

const PROJECT_REPOS: ProjectRepo[] = [
  { projectId: "proj-001", projectName: "Core Platform", repoUrl: "https://gitlab.com/team/core-platform.git", branch: "main", iterationId: "sprint-001-a", iterationName: "Core Platform Sprint 24.10", iterationKey: "CP-24.10" },
  { projectId: "proj-002", projectName: "Mobile App", repoUrl: "https://github.com/team/mobile-app.git", branch: "develop", iterationId: "sprint-002-a", iterationName: "Mobile App Sprint 18", iterationKey: "MA-18" },
];

const SCHEDULED_TASKS: ScheduledTask[] = [
  {
    id: "daily-defect-scan-abc12345",
    name: "Daily Defect Scan",
    enabled: true,
    cronExpr: "0 9 * * *",
    projectId: "proj-001",
    itemType: "defect",
    action: "plan",
    notifyEmails: "dev@example.com,lead@example.com",
    notifyWechat: true,
    lastRunAt: new Date(Date.now() - 3600000).toISOString(),
    lastRunCount: 5,
    createdAt: new Date(Date.now() - 86400000 * 7).toISOString(),
    updatedAt: new Date(Date.now() - 3600000).toISOString(),
  },
  {
    id: "weekly-requirement-review-def67890",
    name: "Weekly Requirement Review",
    enabled: true,
    cronExpr: "0 10 * * 1",
    projectId: "",
    itemType: "requirement",
    action: "analyze",
    notifyEmails: "",
    notifyWechat: true,
    lastRunAt: "",
    lastRunCount: 0,
    createdAt: new Date(Date.now() - 86400000 * 3).toISOString(),
    updatedAt: new Date(Date.now() - 86400000 * 3).toISOString(),
  },
];

const SCHEDULED_TASK_RUNS: ScheduledTaskRun[] = [
  {
    id: "run-001",
    taskId: "daily-defect-scan-abc12345",
    taskName: "Daily Defect Scan",
    taskCronExpr: "0 9 * * *",
    taskAction: "plan",
    taskProjectId: "proj-001",
    status: "success",
    itemCount: 2,
    startedAt: new Date(Date.now() - 3600000).toISOString(),
    finishedAt: new Date(Date.now() - 3540000).toISOString(),
    errorMessage: "",
    createdAt: new Date(Date.now() - 3600000).toISOString(),
  },
  {
    id: "run-002",
    taskId: "weekly-requirement-review-def67890",
    taskName: "Weekly Requirement Review",
    taskCronExpr: "0 10 * * 1",
    taskAction: "analyze",
    taskProjectId: "",
    status: "partial",
    itemCount: 1,
    startedAt: new Date(Date.now() - 7200000).toISOString(),
    finishedAt: new Date(Date.now() - 7140000).toISOString(),
    errorMessage: "1 item failed during analysis",
    createdAt: new Date(Date.now() - 7200000).toISOString(),
  },
];

const SCHEDULED_TASK_RUN_ITEMS: ScheduledTaskRunItem[] = [
  {
    id: "run-item-001",
    runId: "run-001",
    taskId: "daily-defect-scan-abc12345",
    itemUuid: "bug-001",
    itemName: "登录页按钮无响应",
    itemType: "缺陷",
    projectId: "proj-001",
    projectName: "Core Platform",
    assignee: "Alice",
    statusName: "处理中",
    priorityName: "高",
    action: "plan",
    planSummary: "定位前端事件绑定与按钮禁用状态逻辑，补充交互测试。",
    planSteps: ["检查按钮 click 绑定", "检查表单校验阻断", "补充交互测试"],
    riskLevel: "medium",
    branchName: "fix/bug-001-login-button",
    requiresHumanApproval: false,
    analysisMarkdown: "",
    withCodebase: false,
    errorMessage: "",
    itemSnapshot: { uuid: "bug-001", name: "登录页按钮无响应" },
    createdAt: new Date(Date.now() - 3600000).toISOString(),
  },
  {
    id: "run-item-002",
    runId: "run-001",
    taskId: "daily-defect-scan-abc12345",
    itemUuid: "bug-002",
    itemName: "导出接口超时",
    itemType: "缺陷",
    projectId: "proj-001",
    projectName: "Core Platform",
    assignee: "Bob",
    statusName: "待处理",
    priorityName: "中",
    action: "plan",
    planSummary: "分析后端 SQL 与分页策略，增加慢查询日志。",
    planSteps: ["复现超时场景", "检查 SQL 执行计划", "增加慢查询监控"],
    riskLevel: "high",
    branchName: "fix/bug-002-export-timeout",
    requiresHumanApproval: true,
    analysisMarkdown: "",
    withCodebase: false,
    errorMessage: "",
    itemSnapshot: { uuid: "bug-002", name: "导出接口超时" },
    createdAt: new Date(Date.now() - 3580000).toISOString(),
  },
  {
    id: "run-item-003",
    runId: "run-002",
    taskId: "weekly-requirement-review-def67890",
    itemUuid: "req-001",
    itemName: "支持批量审批",
    itemType: "需求",
    projectId: "proj-002",
    projectName: "Mobile App",
    assignee: "Carol",
    statusName: "分析中",
    priorityName: "高",
    action: "analyze",
    planSummary: "",
    planSteps: [],
    riskLevel: "",
    branchName: "",
    requiresHumanApproval: false,
    analysisMarkdown: "### 分析结果\n建议先梳理审批流权限边界，再拆分批量接口和前端交互。",
    withCodebase: true,
    errorMessage: "1 dependent service unavailable",
    itemSnapshot: { uuid: "req-001", name: "支持批量审批" },
    createdAt: new Date(Date.now() - 7190000).toISOString(),
  },
];

export const handlers = [
  http.post("/api/v1/auth/login", async () => {
    await delay(500);
    return HttpResponse.json({
      token: "mock-jwt-token-xxx",
      user: { id: "u1", name: "Admin User", role: "admin" },
    });
  }),

  http.get("/api/v1/auth/me", () => {
    return HttpResponse.json({ id: "u1", name: "Admin User", role: "admin" });
  }),

  http.get("/api/v1/tasks", async ({ request }) => {
    await delay(200);
    const url = new URL(request.url);
    const page = Number(url.searchParams.get("page") || 1);
    const pageSize = Number(url.searchParams.get("pageSize") || 20);
    const status = url.searchParams.get("status");
    const type = url.searchParams.get("type");
    const projectId = url.searchParams.get("projectId");
    const search = url.searchParams.get("search");

    let filtered = [...ITEMS];
    if (status) filtered = filtered.filter((t) => t.status === status);
    if (type) filtered = filtered.filter((t) => t.type === type);
    if (projectId) filtered = filtered.filter((t) => t.projectId === projectId);
    if (search) filtered = filtered.filter((t) => t.title.toLowerCase().includes(search.toLowerCase()));

    const start = (page - 1) * pageSize;
    return HttpResponse.json({
      items: filtered.slice(start, start + pageSize),
      total: filtered.length,
      page,
      pageSize,
    });
  }),

  http.get("/api/v1/tasks/:id", async ({ params }) => {
    await delay(100);
    const item = ITEMS.find((t) => t.id === params.id);
    if (!item) return new HttpResponse(null, { status: 404 });
    return HttpResponse.json(item);
  }),

  http.post("/api/v1/tasks/:id/action", async ({ params, request }) => {
    await delay(300);
    const item = ITEMS.find((t) => t.id === params.id);
    if (!item) return new HttpResponse(null, { status: 404 });
    const body = await request.json() as Partial<TaskActionPayload>;
    if (body.action && body.action in ACTION_STATUS) {
      item.status = ACTION_STATUS[body.action];
      item.requiresApproval = body.action === "pause";
      item.updatedAt = new Date().toISOString();
    }
    return HttpResponse.json(item);
  }),

  http.post("/api/v1/defects/:id/branch", async ({ params, request }) => {
    await delay(500);
    const item = ITEMS.find((t) => t.id === params.id);
    if (!item) return new HttpResponse(null, { status: 404 });
    const body = await request.json() as { branchName?: string; baseBranch?: string; reason?: string };
    item.branch = body.branchName || item.suggestedBranchName || item.branch || `fix/${params.id}`;
    item.baseBranch = body.baseBranch || item.baseBranch || "main";
    item.executionStatus = "created";
    item.executionBranch = item.branch;
    item.executionId = `exec-${item.id}`;
    item.executionRequestedAt = new Date().toISOString();
    item.updatedAt = new Date().toISOString();
    return HttpResponse.json(item);
  }),

  http.get("/api/v1/metrics/summary", async () => {
    await delay(150);
    return HttpResponse.json(METRICS);
  }),

  http.get("/api/v1/logs", async ({ request }) => {
    await delay(200);
    const url = new URL(request.url);
    const page = Number(url.searchParams.get("page") || 1);
    const pageSize = Number(url.searchParams.get("pageSize") || 20);
    const level = url.searchParams.get("level");

    let filtered = [...LOGS];
    if (level) filtered = filtered.filter((l) => l.level === level);

    const start = (page - 1) * pageSize;
    return HttpResponse.json({
      items: filtered.slice(start, start + pageSize),
      total: filtered.length,
      page,
      pageSize,
    });
  }),

  http.get("/api/v1/config", async () => {
    await delay(100);
    return HttpResponse.json(CONFIG);
  }),

  http.put("/api/v1/config", async ({ request }) => {
    await delay(300);
    const body = await request.json() as Partial<AppConfig>;
    Object.assign(CONFIG, body);
    return HttpResponse.json(CONFIG);
  }),

  http.post("/api/v1/config/test/:section", async () => {
    await delay(800);
    return HttpResponse.json({ ok: true, message: "Connection successful" });
  }),

  http.get("/api/v1/ones/projects", async () => {
    await delay(300);
    return HttpResponse.json(ONES_PROJECTS);
  }),

  http.get("/api/v1/ones/team-members", async () => {
    await delay(180);
    return HttpResponse.json(ONES_TEAM_MEMBERS);
  }),

  http.get("/api/v1/ones/projects/:projectId/iterations", async ({ params }) => {
    await delay(240);
    return HttpResponse.json(ONES_ITERATIONS[String(params.projectId)] || []);
  }),

  http.get("/api/v1/project-repos", async () => {
    await delay(100);
    return HttpResponse.json(PROJECT_REPOS);
  }),

  http.post("/api/v1/project-repos", async ({ request }) => {
    await delay(200);
    const body = await request.json() as Omit<ProjectRepo, "projectName"> & { projectName?: string };
    const mapping: ProjectRepo = {
      projectId: body.projectId,
      projectName: body.projectName || "",
      repoUrl: body.repoUrl,
      branch: body.branch || "main",
      iterationId: body.iterationId,
      iterationName: body.iterationName,
      iterationKey: body.iterationKey,
    };
    PROJECT_REPOS.push(mapping);
    return HttpResponse.json(mapping);
  }),

  http.delete("/api/v1/project-repos", async ({ request }) => {
    await delay(200);
    const body = await request.json() as { projectId: string; repoUrl: string };
    const idx = PROJECT_REPOS.findIndex((m) => m.projectId === body.projectId && m.repoUrl === body.repoUrl);
    if (idx >= 0) PROJECT_REPOS.splice(idx, 1);
    return HttpResponse.json({ ok: true });
  }),

  http.get("/api/v1/defects", async ({ request }) => {
    await delay(240);
    const url = new URL(request.url);
    const page = Number(url.searchParams.get("page") || 1);
    const pageSize = Number(url.searchParams.get("pageSize") || 12);
    const projectId = url.searchParams.get("projectId") || "";
    const assignee = (url.searchParams.get("assignee") || "").toLowerCase();
    const status = url.searchParams.get("status") || "";
    const analysisStatus = url.searchParams.get("analysisStatus") || "";
    const mappingStatus = url.searchParams.get("mappingStatus") || "";
    const search = (url.searchParams.get("search") || "").toLowerCase();

    let filtered = [...DEFECTS];
    if (projectId) filtered = filtered.filter((defect) => defect.projectId === projectId);
    if (assignee) filtered = filtered.filter((defect) => (defect.assignee || "").toLowerCase().includes(assignee));
    if (status) filtered = filtered.filter((defect) => defect.status === status);
    if (analysisStatus) filtered = filtered.filter((defect) => defect.analysisStatus === analysisStatus);
    if (mappingStatus) filtered = filtered.filter((defect) => defect.mappingStatus === mappingStatus);
    if (search) {
      filtered = filtered.filter((defect) =>
        [defect.title, defect.onesId, defect.projectName, defect.analysisSummary ?? "", defect.rootCause ?? ""].some((value) => value.toLowerCase().includes(search))
      );
    }

    const start = (page - 1) * pageSize;
    return HttpResponse.json({
      items: filtered.slice(start, start + pageSize),
      total: filtered.length,
      page,
      pageSize,
    });
  }),

  http.get("/api/v1/defects/:id", async ({ params }) => {
    await delay(120);
    const defect = DEFECTS.find((item) => item.id === params.id);
    if (!defect) return new HttpResponse(null, { status: 404 });
    return HttpResponse.json(defect);
  }),

  http.post("/api/v1/defects/:id/execution", async ({ params, request }) => {
    await delay(450);
    const defect = DEFECTS.find((item) => item.id === params.id);
    if (!defect) return new HttpResponse(null, { status: 404 });

    if (defect.mappingStatus === "missing" || defect.analysisStatus !== "analyzed") {
      return HttpResponse.json({ message: "Defect is not ready for branch creation" }, { status: 409 });
    }

    const body = await request.json() as DefectExecutionRequest;
    const requestType = body.requestType || "bugfix";
    const baseBranch = body.baseBranch || defect.selectedRepo?.branch || defect.baseBranch || "main";
    const branchName = body.branchName || defect.suggestedBranchName || `${requestType === "bugfix" ? "fix" : "feat"}/${defect.onesId.toLowerCase()}`;
    const repoUrl = defect.selectedRepo?.repoUrl || defect.execution?.repoUrl || `https://github.com/example/${defect.projectName.toLowerCase().replace(/\s+/g, "-")}.git`;
    const branchUrl = `${repoUrl.replace(/\.git$/, "")}/tree/${branchName}`;
    const updatedAt = new Date().toISOString();

    defect.execution = {
      status: "created",
      requestType,
      repoUrl,
      baseBranch,
      branchName,
      branchUrl,
      message: body.notes || "Branch created from defect analysis",
      updatedAt,
    };
    defect.executionStatus = "created";
    defect.executionBranch = branchName;
    defect.executionRequestedAt = updatedAt;
    defect.branch = branchName;
    defect.baseBranch = baseBranch;
    defect.updatedAt = updatedAt;

    return HttpResponse.json(defect);
  }),

  http.get("/api/v1/scheduled-tasks", async () => {
    await delay(200);
    return HttpResponse.json(SCHEDULED_TASKS);
  }),

  http.post("/api/v1/scheduled-tasks", async ({ request }) => {
    await delay(300);
    const body = await request.json() as ScheduledTaskCreatePayload;
    const task: ScheduledTask = {
      id: `${body.name?.toLowerCase().replace(/\s+/g, "-")}-${Math.random().toString(36).slice(2, 10)}`,
      name: body.name || "New Task",
      enabled: body.enabled ?? true,
      cronExpr: body.cronExpr || "0 9 * * *",
      projectId: body.projectId || "",
      assigneeId: (body as ScheduledTask).assigneeId || "",
      assigneeName: (body as ScheduledTask).assigneeName || "",
      itemType: body.itemType || "all",
      action: body.action || "plan",
      notifyEmails: body.notifyEmails || "",
      notifyWechat: body.notifyWechat ?? false,
      lastRunAt: "",
      lastRunCount: 0,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };
    SCHEDULED_TASKS.push(task);
    return HttpResponse.json(task);
  }),

  http.put("/api/v1/scheduled-tasks/:id", async ({ params, request }) => {
    await delay(300);
    const task = SCHEDULED_TASKS.find((t) => t.id === params.id);
    if (!task) return new HttpResponse(null, { status: 404 });
    const body = await request.json() as Partial<ScheduledTask>;
    Object.assign(task, body, { updatedAt: new Date().toISOString() });
    return HttpResponse.json(task);
  }),

  http.delete("/api/v1/scheduled-tasks/:id", async ({ params }) => {
    await delay(200);
    const idx = SCHEDULED_TASKS.findIndex((t) => t.id === params.id);
    if (idx >= 0) SCHEDULED_TASKS.splice(idx, 1);
    return HttpResponse.json({ ok: true });
  }),

  http.post("/api/v1/scheduled-tasks/:id/trigger", async () => {
    await delay(1000);
    return HttpResponse.json({ triggered: true, taskId: "mock", count: 3 });
  }),

  http.get("/api/v1/scheduled-tasks/:id/runs", async ({ params }) => {
    await delay(200);
    return HttpResponse.json(SCHEDULED_TASK_RUNS.filter((run) => run.taskId === params.id));
  }),

  http.get("/api/v1/scheduled-task-runs", async ({ request }) => {
    await delay(200);
    const url = new URL(request.url);
    const taskId = url.searchParams.get("taskId") || "";
    const status = url.searchParams.get("status") || "";
    const search = (url.searchParams.get("search") || "").toLowerCase();
    const page = Number(url.searchParams.get("page") || 1);
    const pageSize = Number(url.searchParams.get("pageSize") || 20);

    let filtered = [...SCHEDULED_TASK_RUNS];
    if (taskId) filtered = filtered.filter((run) => run.taskId === taskId);
    if (status) filtered = filtered.filter((run) => run.status === status);
    if (search) {
      filtered = filtered.filter((run) => {
        const itemMatches = SCHEDULED_TASK_RUN_ITEMS.some((item) =>
          item.runId === run.id && [item.itemUuid, item.itemName, item.planSummary, item.analysisMarkdown].some((value) => value.toLowerCase().includes(search))
        );
        return [run.taskId, run.taskName || ""].some((value) => value.toLowerCase().includes(search)) || itemMatches;
      });
    }

    const start = (page - 1) * pageSize;
    return HttpResponse.json({
      items: filtered.slice(start, start + pageSize),
      total: filtered.length,
      page,
      pageSize,
    });
  }),

  http.get("/api/v1/scheduled-task-runs/:id/items", async ({ params }) => {
    await delay(200);
    return HttpResponse.json(SCHEDULED_TASK_RUN_ITEMS.filter((item) => item.runId === params.id));
  }),

  http.post("/api/v1/ai/trigger", async () => {
    await delay(2000);
    return HttpResponse.json({
      itemId: "mock-item",
      name: "Mock Item",
      action: "plan",
      plan: { summary: "AI generated plan", steps: ["Step 1", "Step 2"], riskLevel: "low", branch: "feat/mock", requiresHumanApproval: false },
    });
  }),
];
