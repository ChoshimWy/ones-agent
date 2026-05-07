import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { useTasks, useTaskAction } from "@/api/queries";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { getStatusMeta, formatRelativeTime, getRiskMeta } from "@/utils/format";
import { CheckCircle, XCircle, Edit3, ShieldCheck } from "lucide-react";

const decisionSchema = z.object({
  reason: z.string().min(1, "Reason is required"),
});

type DecisionForm = z.infer<typeof decisionSchema>;

export default function Approval() {
  const { data, isLoading } = useTasks({ status: "WAITING_APPROVAL" });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [decisionType, setDecisionType] = useState<"approve" | "reject" | "modify" | null>(null);

  const tasks = data?.items ?? [];
  const selectedTask = tasks.find((t) => t.id === selectedId);

  const actionMutation = useTaskAction(selectedId ?? "");

  const {
    register,
    handleSubmit,
    formState: { errors },
    reset,
  } = useForm<DecisionForm>({ resolver: zodResolver(decisionSchema) });

  function onSubmit(values: DecisionForm) {
    if (!decisionType) return;
    actionMutation.mutate(
      {
        action: decisionType === "approve" ? "approve" : "reject",
        reason: values.reason,
      },
      {
        onSuccess: () => {
          setSelectedId(null);
          setDecisionType(null);
          reset();
        },
      }
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <ShieldCheck className="h-5 w-5" />
        <h1 className="text-2xl font-bold tracking-tight">Approval Desk</h1>
        <Badge variant="outline">{tasks.length} pending</Badge>
      </div>

      {isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full" />
          ))}
        </div>
      ) : tasks.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-muted-foreground">
            No tasks pending approval
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[1fr_2fr]">
          {/* List */}
          <div className="space-y-2">
            {tasks.map((task) => {
              const sm = getStatusMeta(task.status);
              const rm = task.riskLevel ? getRiskMeta(task.riskLevel) : null;
              return (
                <Card
                  key={task.id}
                  className={`cursor-pointer transition-colors hover:bg-accent ${
                    selectedId === task.id ? "ring-2 ring-primary" : ""
                  }`}
                  onClick={() => setSelectedId(task.id)}
                >
                  <CardContent className="p-3">
                    <div className="flex items-center gap-2">
                      <Badge variant={sm.variant as "default" | "secondary" | "destructive" | "outline"}>
                        {sm.icon} {sm.label}
                      </Badge>
                      {rm && (
                        <Badge variant={rm.variant as "default" | "secondary" | "destructive" | "outline"}>
                          {rm.label}
                        </Badge>
                      )}
                    </div>
                    <p className="mt-1 text-sm font-medium truncate">{task.title}</p>
                    <p className="text-xs text-muted-foreground">{formatRelativeTime(task.updatedAt)}</p>
                  </CardContent>
                </Card>
              );
            })}
          </div>

          {/* Detail */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">
                {selectedTask ? selectedTask.title : "Select a task to review"}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {selectedTask ? (
                <div className="space-y-4">
                  <Tabs defaultValue="plan">
                    <TabsList>
                      <TabsTrigger value="plan">LLM Plan</TabsTrigger>
                      <TabsTrigger value="changes">Expected Changes</TabsTrigger>
                      <TabsTrigger value="diff">Diff Preview</TabsTrigger>
                    </TabsList>

                    <TabsContent value="plan" className="mt-4">
                      <pre className="rounded-md bg-muted p-4 text-xs overflow-auto max-h-64">
                        {selectedTask.planJson
                          ? JSON.stringify(selectedTask.planJson, null, 2)
                          : "No plan data available"}
                      </pre>
                    </TabsContent>

                    <TabsContent value="changes" className="mt-4">
                      <div className="text-sm text-muted-foreground">
                        {selectedTask.branch ? (
                          <p>Branch: <code className="rounded bg-muted px-1 py-0.5 text-xs">{selectedTask.branch}</code></p>
                        ) : (
                          <p>No branch information</p>
                        )}
                      </div>
                    </TabsContent>

                    <TabsContent value="diff" className="mt-4">
                      <div className="rounded-md border bg-muted p-4 text-xs text-muted-foreground">
                        Diff preview will show file changes once the plan is executed.
                      </div>
                    </TabsContent>
                  </Tabs>

                  <Separator />

                  <div className="flex gap-2">
                    <Button
                      variant="default"
                      className="gap-1"
                      onClick={() => {
                        setDecisionType("approve");
                        reset();
                      }}
                    >
                      <CheckCircle className="h-4 w-4" /> Approve
                    </Button>
                    <Button
                      variant="destructive"
                      className="gap-1"
                      onClick={() => {
                        setDecisionType("reject");
                        reset();
                      }}
                    >
                      <XCircle className="h-4 w-4" /> Reject
                    </Button>
                    <Button
                      variant="outline"
                      className="gap-1"
                      onClick={() => {
                        setDecisionType("modify");
                        reset();
                      }}
                    >
                      <Edit3 className="h-4 w-4" /> Request Modification
                    </Button>
                  </div>
                </div>
              ) : (
                <p className="text-muted-foreground text-sm">
                  Select a task from the list to view details and make a decision.
                </p>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {/* Decision Dialog */}
      <Dialog open={!!decisionType} onOpenChange={(open) => !open && setDecisionType(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {decisionType === "approve" ? "Approve Task" : decisionType === "reject" ? "Reject Task" : "Request Modification"}
            </DialogTitle>
            <DialogDescription>
              Please provide a reason for your decision.
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="reason">Reason</Label>
              <Input id="reason" {...register("reason")} placeholder="Enter your reason..." />
              {errors.reason && (
                <p className="text-xs text-destructive">{errors.reason.message}</p>
              )}
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setDecisionType(null)}>Cancel</Button>
              <Button
                type="submit"
                disabled={actionMutation.isPending}
                variant={decisionType === "approve" ? "default" : decisionType === "reject" ? "destructive" : "outline"}
              >
                {actionMutation.isPending ? "Processing..." : decisionType === "approve" ? "Confirm Approval" : decisionType === "reject" ? "Confirm Rejection" : "Submit"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}