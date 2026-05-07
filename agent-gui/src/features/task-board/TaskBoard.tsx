import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useTasks, useTaskAction } from "@/api/queries";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { getStatusMeta, formatRelativeTime, getRiskMeta } from "@/utils/format";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Search, Bug, FileText, Play, Pause, RotateCcw, X, Cpu } from "lucide-react";
import { useAiTrigger } from "@/api/queries";

const STATUS_OPTIONS = ["All Statuses", "PENDING", "PLANNING", "CODING", "TESTING", "WAITING_APPROVAL", "SUCCESS", "FAILED"];
const TYPE_OPTIONS = ["All Types", "requirement", "defect"];

export default function TaskBoard() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [search, setSearch] = useState(searchParams.get("search") || "");
  const debouncedSearch = useDebouncedValue(search, 300);
  const [confirmAction, setConfirmAction] = useState<{ id: string; action: string } | null>(null);

  const status = searchParams.get("status") ?? "";
  const type = searchParams.get("type") ?? "";
  const page = Number(searchParams.get("page") ?? "1");

  function setFilter(key: string, value: string) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "page") next.delete("page");
    setSearchParams(next, { replace: true });
  }

  const { data, isLoading } = useTasks({
    status: status || undefined,
    type: type || undefined,
    search: debouncedSearch || undefined,
    page,
    pageSize: 20,
  });

  const actionMutation = useTaskAction(confirmAction?.id || "");

  function handleConfirm() {
    if (!confirmAction) return;
    actionMutation.mutate(
      { action: confirmAction.action as "pause" | "resume" | "retry" | "cancel" | "approve" | "reject" },
      { onSettled: () => setConfirmAction(null) }
    );
  }

  const ACTION_BTNS = [
      { action: "pause", label: "Pause", icon: Pause, roles: ["PENDING", "PARSING", "PLANNING", "CODING", "TESTING", "PUSHING", "REPORTING"] as string[] },
      { action: "resume", label: "Resume", icon: Play, roles: ["WAITING_APPROVAL"] as string[] },
      { action: "retry", label: "Retry", icon: RotateCcw, roles: ["FAILED"] as string[] },
      { action: "cancel", label: "Cancel", icon: X, roles: ["PENDING", "PARSING", "PLANNING", "WAITING_APPROVAL", "CODING", "TESTING", "PUSHING", "REPORTING"] as string[] },
  ] as const;

  // AI Trigger state per-task
  const [aiTaskId, setAiTaskId] = useState<string | null>(null);
  const aiMutation = useAiTrigger();
  const [aiForm, setAiForm] = useState<{ action: "plan" | "analyze"; notifyEmails: string; notifyWechat: boolean } | null>(null);

  // Keep a reference to the current task for AI dialog header
  const currentTaskForAi = data?.items.find((t) => t.id === aiTaskId) ?? null;

  function openAiDialog(id: string) {
    setAiTaskId(id);
    setAiForm({ action: "plan", notifyEmails: "", notifyWechat: false });
  }
  function closeAiDialog() {
    setAiTaskId(null);
    setAiForm(null);
  }
  function triggerAi() {
    if (!aiTaskId || !aiForm) return;
    const payload = {
      itemId: aiTaskId,
      action: aiForm.action,
      notifyEmails: aiForm.notifyEmails,
      notifyWechat: aiForm.notifyWechat,
    };
    aiMutation.mutate(payload, {
      onSuccess: () => {
        closeAiDialog();
        import("sonner").then(({ toast }) => {
          toast.success("AI Triggered", { description: `Task ${aiTaskId} AI ${payload.action} triggered` });
        });
      },
      onError: (e: unknown) => {
        closeAiDialog();
        import("sonner").then(({ toast }) => {
          const message = e instanceof Error ? e.message : "Unknown error";
          toast.error("AI Trigger Failed", { description: message });
        });
      },
    });
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">Tasks</h1>
        <Badge variant="secondary">{data?.total ?? "—"} total</Badge>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-64">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search tasks..."
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setFilter("search", e.target.value);
            }}
            className="pl-9"
          />
        </div>
        <Select value={status || "All Statuses"} onValueChange={(v) => setFilter("status", (v === "All Statuses" ? "" : v ?? ""))}>
          <SelectTrigger className="w-40"><SelectValue placeholder="Status" /></SelectTrigger>
          <SelectContent>
            {STATUS_OPTIONS.map((s) => (
              <SelectItem key={s} value={s}>{s}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={type || "All Types"} onValueChange={(v) => setFilter("type", (v === "All Types" ? "" : v ?? ""))}>
          <SelectTrigger className="w-36"><SelectValue placeholder="Type" /></SelectTrigger>
          <SelectContent>
            {TYPE_OPTIONS.map((t) => (
              <SelectItem key={t} value={t}>{t}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Table */}
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-10">Type</TableHead>
              <TableHead>ONES ID</TableHead>
              <TableHead>Title</TableHead>
              <TableHead>Branch</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Risk</TableHead>
              <TableHead>Updated</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              Array.from({ length: 5 }).map((_, i) => (
                <TableRow key={i}>
                  {Array.from({ length: 8 }).map((_, j) => (
                    <TableCell key={j}><Skeleton className="h-5 w-full" /></TableCell>
                  ))}
                </TableRow>
              ))
            ) : (
              data?.items.map((task) => {
                const sm = getStatusMeta(task.status);
                const rm = task.riskLevel ? getRiskMeta(task.riskLevel) : null;
                return (
                  <TableRow key={task.id}>
                    <TableCell>
                      {task.type === "defect" ? (
                        <Bug className="h-4 w-4 text-destructive" />
                      ) : (
                        <FileText className="h-4 w-4 text-muted-foreground" />
                      )}
                    </TableCell>
                    <TableCell className="font-mono text-xs">{task.onesId}</TableCell>
                    <TableCell className="max-w-[200px] truncate">{task.title}</TableCell>
                    <TableCell className="font-mono text-xs max-w-[160px] truncate">
                      {task.branch || "—"}
                    </TableCell>
                    <TableCell>
                      <Badge variant={sm.variant as "default" | "secondary" | "destructive" | "outline"}>
                        {sm.icon} {sm.label}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {rm ? (
                        <Badge variant={rm.variant as "default" | "secondary" | "destructive" | "outline"}>
                          {rm.label}
                        </Badge>
                      ) : "—"}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {formatRelativeTime(task.updatedAt)}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1">
                        {ACTION_BTNS.filter((b) => b.roles.includes(task.status)).map((b) => (
                          <Button
                            key={b.action}
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7"
                            title={b.label}
                            onClick={() => setConfirmAction({ id: task.id, action: b.action })}
                          >
                            <b.icon className="h-3.5 w-3.5" />
                          </Button>
                        ))}
                        {/* AI Trigger Button */}
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          title="AI Trigger"
                          onClick={() => openAiDialog(task.id)}
                        >
                          <Cpu className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>

      {/* Pagination */}
      {data && data.total > 20 && (
        <div className="flex items-center justify-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={page <= 1}
            onClick={() => setFilter("page", String(page - 1))}
          >
            Previous
          </Button>
          <span className="text-sm text-muted-foreground">
            Page {page} of {Math.ceil(data.total / 20)}
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={page * 20 >= data.total}
            onClick={() => setFilter("page", String(page + 1))}
          >
            Next
          </Button>
        </div>
      )}

      {/* AI Trigger Dialog */}
      <Dialog open={Boolean(aiTaskId)} onOpenChange={(open) => { if (!open) closeAiDialog(); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>AI Trigger</DialogTitle>
            <DialogDescription>Trigger AI action for: {currentTaskForAi?.title ?? ''}</DialogDescription>
          </DialogHeader>
          <div className="space-y-2 p-2">
            <div className="flex items-center gap-2">
              <span className={aiForm?.action === 'plan' ? 'px-2 py-1 rounded bg-muted' : 'px-2 py-1 rounded'}>Plan</span>
              <Button type="button" size="sm" variant={aiForm?.action === 'analyze' ? 'default' : 'outline'} onClick={() => setAiForm((f) => f ? { ...f, action: 'analyze' } : { action: 'analyze', notifyEmails: '', notifyWechat: false })}>Analyze</Button>
              <Button type="button" size="sm" variant={aiForm?.action === 'plan' ? 'default' : 'outline'} onClick={() => setAiForm((f) => f ? { ...f, action: 'plan' } : { action: 'plan', notifyEmails: '', notifyWechat: false })}>Plan</Button>
            </div>
            <Input placeholder="Notify emails (comma separated)" value={aiForm?.notifyEmails ?? ''} onChange={(e) => setAiForm((f) => f ? { ...f, notifyEmails: e.target.value } : { action: 'plan', notifyEmails: e.target.value, notifyWechat: false })} />
            <div className="flex items-center gap-2">
              <label className="flex items-center space-x-2">
                <input type="checkbox" checked={aiForm?.notifyWechat ?? false} onChange={(e) => setAiForm((f) => f ? { ...f, notifyWechat: e.target.checked } : { action: 'plan', notifyEmails: '', notifyWechat: e.target.checked })} />
                <span>Notify WeChat</span>
              </label>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeAiDialog}>Cancel</Button>
            <Button onClick={triggerAi}>Trigger</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {/* Confirm Dialog */}
      <Dialog open={!!confirmAction} onOpenChange={(open) => !open && setConfirmAction(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Confirm Action</DialogTitle>
            <DialogDescription>
              Are you sure you want to {confirmAction?.action} this task?
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmAction(null)}>Cancel</Button>
            <Button onClick={handleConfirm} disabled={actionMutation.isPending}>
              {actionMutation.isPending ? "Processing..." : "Confirm"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      
    </div>
  );
}
