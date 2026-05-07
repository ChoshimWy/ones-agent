import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useScheduledTasks, useCreateScheduledTask, useDeleteScheduledTask, useTriggerScheduledTask, useOnesProjects, useOnesTeamMembers, useScheduledTaskRuns, useScheduledTaskRunItems } from "@/api/queries";
import type { ScheduledTask, ScheduledTaskRun, ScheduledTaskRunItem } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Loader2 } from "lucide-react";
import { formatRelativeTime } from "@/utils/format";

export default function ScheduledTasksPage() {
  const { data: tasks, isLoading } = useScheduledTasks();
  const { data: onesProjects, isLoading: projectsLoading, error: projectsError } = useOnesProjects();
  const createMutation = useCreateScheduledTask();
  const deleteMutation = useDeleteScheduledTask();
  const [showForm, setShowForm] = useState(false);
  const emptyForm: ScheduledTaskForm = useMemo(
    () => ({
      name: "",
      cronExpr: "0 9 * * *",
      projectId: "",
      assigneeId: "",
      assigneeName: "",
      itemType: "all",
      action: "plan",
      notifyEmails: "",
      notifyWechat: false,
      enabled: true,
    }),
    []
  );
  const [form, setForm] = useState<ScheduledTaskForm>(emptyForm);
  const { data: teamMembers, isLoading: teamMembersLoading } = useOnesTeamMembers(form.projectId || undefined);
  const selectedProject = onesProjects?.find((project) => project.id === form.projectId) ?? null;
  const assigneePlaceholder = !form.projectId
    ? "Select a project first"
    : teamMembersLoading
      ? "Loading members..."
      : teamMembers && teamMembers.length > 0
        ? "Current User"
        : `No members found for ${selectedProject?.name || "this project"}`;

  useEffect(() => {
    if (!form.assigneeId) {
      return;
    }

    if (!teamMembers?.some((member) => member.id === form.assigneeId)) {
      setForm((current) => ({
        ...current,
        assigneeId: "",
        assigneeName: "",
      }));
    }
  }, [form.assigneeId, teamMembers]);

  function handleProjectChange(projectId: string) {
    setForm((current) => {
      if (current.projectId === projectId) {
        return current;
      }

      return {
        ...current,
        projectId,
        assigneeId: "",
        assigneeName: "",
      };
    });
  }

  function handleAssigneeChange(assigneeId: string) {
    const member = teamMembers?.find((item) => item.id === assigneeId);
    setForm((current) => ({
      ...current,
      assigneeId,
      assigneeName: member?.name || "",
    }));
  }

  function handleCreate() {
    if (!form.name || !form.cronExpr) return;
    createMutation.mutate(form, {
      onSuccess: () => {
        setShowForm(false);
        setForm(emptyForm);
      },
    });
  }

  if (isLoading) {
    return (
      <div className="flex justify-center py-20">
        <Loader2 className="h-6 w-6 animate-spin" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">Scheduled Tasks</h1>
        <Button size="sm" onClick={() => setShowForm(!showForm)}>
          {showForm ? "Cancel" : "+ Add Task"}
        </Button>
      </div>

      <p className="text-sm text-muted-foreground">
        Configure periodic scans of ONES defects/requirements. AI will analyze or
        plan, then notify via email or WeChat.
      </p>

      {showForm && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">New Scheduled Task</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid gap-3 md:grid-cols-3">
              <div className="space-y-1">
                <Label>Name</Label>
                <Input
                  value={form.name}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, name: e.target.value }))
                  }
                  placeholder="Daily Defect Scan"
                />
              </div>
              <div className="space-y-1">
                <Label>Cron / Interval</Label>
                <Input
                  value={form.cronExpr}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, cronExpr: e.target.value }))
                  }
                  placeholder="0 9 * * * or 30m or 1h"
                />
              </div>
              <div className="space-y-1">
                <Label>Project</Label>
                {projectsLoading ? (
                  <div className="flex items-center h-9 px-3 text-sm text-muted-foreground">
                    <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />Loading projects...
                  </div>
                ) : projectsError ? (
                  <Input
                    value={form.projectId}
                    onChange={(e) =>
                      handleProjectChange(e.target.value)
                    }
                    placeholder="Enter Project ID manually"
                  />
                ) : (
                  <select
                    className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    value={form.projectId}
                    onChange={(e) =>
                      handleProjectChange(e.target.value)
                    }
                  >
                    <option value="">All Projects</option>
                    {onesProjects?.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name} ({p.id})
                      </option>
                    ))}
                  </select>
                )}
              </div>
              <div className="space-y-1">
                <Label>Item Type</Label>
                <select
                  className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
                  value={form.itemType}
                  onChange={(e) =>
                      setForm((f) => ({ ...f, itemType: e.target.value as ScheduledTaskForm["itemType"] }))
                  }
                >
                  <option value="all">All</option>
                  <option value="defect">Defect Only</option>
                  <option value="requirement">Requirement Only</option>
                </select>
              </div>
              <div className="space-y-1">
                <Label>Assignee</Label>
                <select
                  className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
                  value={form.assigneeId}
                  onChange={(e) => handleAssigneeChange(e.target.value)}
                  disabled={!form.projectId || teamMembersLoading}
                >
                  <option value="">{assigneePlaceholder}</option>
                  {teamMembers?.map((member) => (
                    <option key={member.id} value={member.id}>
                      {member.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="space-y-1">
                <Label>Action</Label>
                <select
                  className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
                  value={form.action}
                  onChange={(e) =>
                      setForm((f) => ({ ...f, action: e.target.value as ScheduledTaskForm["action"] }))
                  }
                >
                  <option value="plan">Plan (开发计划)</option>
                  <option value="analyze">Analyze (分析建议)</option>
                </select>
              </div>
              <div className="space-y-1">
                <Label>Notify Emails</Label>
                <Input
                  value={form.notifyEmails}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, notifyEmails: e.target.value }))
                  }
                  placeholder="a@b.com, c@d.com"
                />
              </div>
              <div className="space-y-1 flex items-end gap-4">
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={form.notifyWechat}
                    onChange={(e) =>
                      setForm((f) => ({ ...f, notifyWechat: e.target.checked }))
                    }
                  />
                  WeChat
                </label>
                <Button
                  size="sm"
                  onClick={handleCreate}
                  disabled={!form.name || createMutation.isPending}
                >
                  {createMutation.isPending ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    "Create"
                  )}
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {tasks && tasks.length > 0 ? (
        <div className="rounded-md border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50">
                <th className="px-3 py-2 text-left font-medium">Name</th>
                <th className="px-3 py-2 text-left font-medium">Cron</th>
                <th className="px-3 py-2 text-left font-medium">Project</th>
                <th className="px-3 py-2 text-left font-medium">Assignee</th>
                <th className="px-3 py-2 text-left font-medium">Type</th>
                <th className="px-3 py-2 text-left font-medium">Action</th>
                <th className="px-3 py-2 text-left font-medium">Notify</th>
                <th className="px-3 py-2 text-left font-medium">Last Run</th>
                <th className="px-3 py-2 w-28"></th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((t: ScheduledTask) => (
                <tr key={t.id} className="border-b last:border-0">
                  <td className="px-3 py-2">
                    <div className="font-medium">{t.name}</div>
                    <div className="font-mono text-xs text-muted-foreground">
                      {t.id}
                    </div>
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">{t.cronExpr}</td>
                  <td className="px-3 py-2 text-xs">
                    {t.projectId
                      ? onesProjects?.find((p) => p.id === t.projectId)?.name || t.projectId
                      : "All"}
                  </td>
                  <td className="px-3 py-2 text-xs">{t.assigneeName || "Current User"}</td>
                  <td className="px-3 py-2">
                    <Badge variant="outline">{t.itemType}</Badge>
                  </td>
                  <td className="px-3 py-2">
                    <Badge variant="secondary">{t.action}</Badge>
                  </td>
                  <td className="px-3 py-2 text-xs">
                    {t.notifyEmails && <span>📧</span>}
                    {t.notifyWechat && <span className="ml-1">💬</span>}
                    {!t.notifyEmails && !t.notifyWechat && "—"}
                  </td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {t.lastRunAt ? formatRelativeTime(t.lastRunAt) : "Never"}
                    {t.lastRunCount ? ` (${t.lastRunCount})` : ""}
                  </td>
                    <td className="px-3 py-2">
                      <div className="flex gap-1">
                        <TriggerButton id={t.id} />
                        <ResultsButton id={t.id} />
                        <Button
                          variant="ghost"
                          size="sm"
                        className="h-7 text-destructive hover:text-destructive"
                        onClick={() => deleteMutation.mutate(t.id)}
                        disabled={deleteMutation.isPending}
                      >
                        Delete
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground text-center py-10">
          No scheduled tasks configured yet.
        </p>
      )}
    </div>
  );
}

function TriggerButton({ id }: { id: string }) {
  const mutation = useTriggerScheduledTask(id);
  return (
    <Button
      variant="outline"
      size="sm"
      className="h-7"
      disabled={mutation.isPending}
      onClick={() => {
        mutation.mutate(undefined, {
          onSuccess: (data) => {
            import("sonner").then(({ toast }) => {
              toast.success("Task Triggered", {
                description: `Found ${data.count} items`,
              });
            });
          },
          onError: (e: any) => {
            import("sonner").then(({ toast }) => {
              toast.error("Trigger Failed", {
                description: e?.message || "Unknown error",
              });
            });
          },
        });
      }}
    >
      {mutation.isPending ? (
        <Loader2 className="h-3 w-3 animate-spin" />
      ) : (
        "▶ Run"
      )}
    </Button>
  );
}

function ResultsButton({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState("");
  const { data: runs, isLoading: runsLoading } = useScheduledTaskRuns(open ? id : "");
  const { data: items, isLoading: itemsLoading } = useScheduledTaskRunItems(selectedRunId);

  useEffect(() => {
    if (!open) {
      setSelectedRunId("");
      return;
    }
    if (!runs || runs.length === 0) return;
    if (!selectedRunId || !runs.some((run) => run.id === selectedRunId)) {
      setSelectedRunId(runs[0].id);
    }
  }, [open, runs, selectedRunId]);

  const selectedRun = runs?.find((run) => run.id === selectedRunId) ?? null;

  return (
    <>
      <Button variant="ghost" size="sm" className="h-7" onClick={() => setOpen(true)}>
        History
      </Button>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogContent className="max-h-[92vh] w-[99vw] max-w-[min(99vw,1760px)] overflow-hidden px-5 lg:px-6">
            <DialogHeader>
              <DialogTitle>Scheduled Task History</DialogTitle>
              <DialogDescription>查看当前定时任务每次执行的历史记录，以及对应命中项和分析/规划结果。</DialogDescription>
            </DialogHeader>
            <RunDetailsLayout
              runs={runs ?? []}
              runsLoading={runsLoading}
              selectedRunId={selectedRunId}
              onSelectRun={setSelectedRunId}
              selectedRun={selectedRun}
              items={items ?? []}
              itemsLoading={itemsLoading}
            />
          </DialogContent>
        </Dialog>
    </>
  );
}

function RunDetailsLayout({
  runs,
  runsLoading,
  selectedRunId,
  onSelectRun,
  selectedRun,
  items,
  itemsLoading,
  runLabel,
  toolbar,
}: {
  runs: ScheduledTaskRun[];
  runsLoading: boolean;
  selectedRunId: string;
  onSelectRun: (runId: string) => void;
  selectedRun: ScheduledTaskRun | null;
  items: ScheduledTaskRunItem[];
  itemsLoading: boolean;
  runLabel?: (run: ScheduledTaskRun) => string;
  toolbar?: ReactNode;
}) {
  return (
    <div className="grid min-h-0 gap-3 md:grid-cols-[168px_minmax(0,1fr)] xl:grid-cols-[188px_minmax(0,1fr)] 2xl:grid-cols-[210px_minmax(0,1fr)]">
      <div className="space-y-2 overflow-y-auto border-r pr-2">
        <div className="text-sm font-medium">Runs</div>
        {runsLoading ? (
          <div className="flex justify-center py-6"><Loader2 className="h-4 w-4 animate-spin" /></div>
        ) : runs.length > 0 ? (
          runs.map((run) => (
            <button
              key={run.id}
              type="button"
              className={`w-full rounded border px-3 py-2 text-left text-sm ${selectedRunId === run.id ? "border-primary bg-muted" : "border-border"}`}
              onClick={() => onSelectRun(run.id)}
            >
              <div className="flex items-center justify-between gap-2">
                <Badge variant={getRunStatusVariant(run.status)}>{run.status}</Badge>
                <span className="text-xs text-muted-foreground">{run.itemCount} items</span>
              </div>
              {runLabel ? <div className="mt-2 line-clamp-2 text-sm font-medium">{runLabel(run)}</div> : null}
              <div className="mt-2 text-xs text-muted-foreground">{formatRelativeTime(run.startedAt)}</div>
              {run.errorMessage ? <div className="mt-1 text-xs text-destructive line-clamp-2">{run.errorMessage}</div> : null}
            </button>
          ))
        ) : (
          <p className="py-6 text-sm text-muted-foreground">No execution history yet.</p>
        )}
      </div>
      <div className="min-w-0 space-y-3 overflow-hidden">
        {selectedRun ? (
          <>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge variant={getRunStatusVariant(selectedRun.status)}>{selectedRun.status}</Badge>
              {runLabel ? <span className="font-medium">{runLabel(selectedRun)}</span> : null}
              <span className="text-muted-foreground">Started {formatRelativeTime(selectedRun.startedAt)}</span>
              <span className="text-muted-foreground">{selectedRun.itemCount} items</span>
              {toolbar ? <div className="ml-auto">{toolbar}</div> : null}
            </div>
            {itemsLoading ? (
              <div className="flex justify-center py-10"><Loader2 className="h-4 w-4 animate-spin" /></div>
            ) : items.length > 0 ? (
              <div className="max-h-[70vh] min-w-0 space-y-3 overflow-auto">
                {items.map((item) => (
                  <Card key={item.id}>
                    <CardContent className="min-w-0 space-y-3 p-4">
                      <div className="flex flex-wrap items-center gap-2">
                        <div className="min-w-0 break-words font-medium">{item.itemName || item.itemUuid}</div>
                        <Badge variant="outline">{item.itemType || "unknown"}</Badge>
                        <Badge variant="secondary">{item.action}</Badge>
                        {item.errorMessage ? <Badge variant="destructive">error</Badge> : null}
                      </div>
                      <div className="grid min-w-0 gap-2 text-xs text-muted-foreground 2xl:grid-cols-2">
                        <div className="break-words">UUID: {item.itemUuid || "—"}</div>
                        <div className="break-words">Project: {item.projectName || item.projectId || "—"}</div>
                        <div className="break-words">Assignee: {item.assignee || "—"}</div>
                        <div className="break-words">Status / Priority: {item.statusName || "—"} / {item.priorityName || "—"}</div>
                      </div>
                      {item.planSummary ? (
                        <div className="min-w-0 space-y-1 text-sm">
                          <div className="font-medium">Plan Summary</div>
                          <p className="break-words">{item.planSummary}</p>
                          {item.planSteps.length > 0 ? (
                            <ol className="list-decimal space-y-1 pl-5 text-sm">
                              {item.planSteps.map((step, index) => <li key={`${item.id}-${index}`} className="break-words">{step}</li>)}
                            </ol>
                          ) : null}
                          <div className="break-words text-xs text-muted-foreground">Risk: {item.riskLevel || "—"} · Branch: {item.branchName || "—"}</div>
                        </div>
                      ) : null}
                      {item.analysisMarkdown ? (
                        <div className="min-w-0 space-y-1 text-sm">
                          <div className="font-medium">Analysis</div>
                          <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded bg-muted p-3 text-xs">{item.analysisMarkdown}</pre>
                        </div>
                      ) : null}
                      {item.errorMessage ? <div className="break-words text-sm text-destructive">{item.errorMessage}</div> : null}
                      <details className="min-w-0 text-xs text-muted-foreground">
                        <summary className="cursor-pointer">ONES Snapshot</summary>
                        <pre className="mt-2 max-w-full overflow-x-auto rounded bg-muted p-3 whitespace-pre text-[11px] leading-5">{JSON.stringify(item.itemSnapshot, null, 2)}</pre>
                      </details>
                    </CardContent>
                  </Card>
                ))}
              </div>
            ) : (
              <p className="py-10 text-sm text-muted-foreground">No item details for this run.</p>
            )}
          </>
        ) : (
          <p className="py-10 text-sm text-muted-foreground">Select a run to inspect detailed results.</p>
        )}
      </div>
    </div>
  );
}

function getRunStatusVariant(status: ScheduledTaskRun["status"]) {
  if (status === "failed") return "destructive" as const;
  if (status === "partial") return "secondary" as const;
  return "outline" as const;
}

type ScheduledTaskForm = Omit<ScheduledTask, "id" | "createdAt" | "updatedAt" | "lastRunAt" | "lastRunCount">;
