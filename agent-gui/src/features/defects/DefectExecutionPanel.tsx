import { useMemo, useState } from "react";
import type { DefectRecord } from "@/api/types";
import { useCreateDefectBranch } from "@/api/queries";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { getExecutionMeta, formatMaybe } from "@/features/defects/defect-meta";

function slugify(value: string) {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .replace(/-{2,}/g, "-")
    .slice(0, 48);
}

function buildBranchName(defect: DefectRecord, requestType: "bugfix" | "development") {
  const prefix = requestType === "bugfix" ? "fix" : "feat";
  const defectKey = slugify(defect.onesId || defect.id);
  const titleKey = slugify(defect.title).slice(0, 24);
  return `${prefix}/${defectKey}${titleKey ? `-${titleKey}` : ""}`;
}

export default function DefectExecutionPanel({ defect }: { defect: DefectRecord }) {
  const [requestType, setRequestType] = useState<"bugfix" | "development">(
    defect.execution?.requestType ?? "bugfix"
  );
  const mutation = useCreateDefectBranch(defect.id);

  const baseBranch = defect.execution?.baseBranch || defect.selectedRepo?.branch || defect.baseBranch || "main";
  const branchName = useMemo(() => buildBranchName(defect, requestType), [defect, requestType]);
  const canCreate = defect.mappingStatus !== "missing" && defect.analysisStatus === "analyzed";
  const execution = defect.execution;
  const executionMeta = getExecutionMeta(execution?.status);

  function handleCreate() {
    mutation.mutate(
      {
        requestType,
        branchName,
        baseBranch,
        notes: defect.analysis?.summary || defect.analysisSummary || defect.rootCause || defect.title,
      },
      {
        onSuccess: (updated) => {
          import("sonner").then(({ toast }) => {
            toast.success("Branch created", {
              description: `${updated.execution?.branchName ?? branchName} is ready from ${formatMaybe(updated.execution?.baseBranch)}`,
            });
          });
        },
        onError: (error: unknown) => {
          import("sonner").then(({ toast }) => {
            toast.error("Branch creation failed", {
              description: error instanceof Error ? error.message : "Unable to create a branch from this defect.",
            });
          });
        },
      }
    );
  }

  return (
    <Card className="h-full">
      <CardHeader>
        <CardTitle className="text-base">Execution boundary</CardTitle>
        <CardDescription>Branch creation stays separate from analysis and is driven from this defect result.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 rounded-xl border bg-muted/30 p-4 text-sm">
          <div className="flex items-center justify-between gap-2">
            <span className="text-muted-foreground">Execution status</span>
            <Badge variant={executionMeta.variant}>{executionMeta.label}</Badge>
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-muted-foreground">Request type</span>
            <Select value={requestType} onValueChange={(value) => setRequestType(value as "bugfix" | "development") }>
              <SelectTrigger className="w-36">
                <SelectValue placeholder="Request type" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="bugfix">Bugfix</SelectItem>
                <SelectItem value="development">Development</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-1">
            <span className="text-muted-foreground">Base branch</span>
            <span className="font-mono text-xs break-all">{formatMaybe(baseBranch)}</span>
          </div>
          <div className="grid gap-1">
            <span className="text-muted-foreground">Branch name</span>
            <span className="font-mono text-xs break-all">{branchName}</span>
          </div>
          {execution?.branchUrl ? (
            <a className="text-xs text-primary underline underline-offset-4" href={execution.branchUrl} target="_blank" rel="noreferrer">
              Open created branch
            </a>
          ) : null}
        </div>

        <div className="grid gap-2 rounded-xl border border-dashed p-4 text-sm text-muted-foreground">
          <p>Branch creation unlocks once mapping is resolved and analysis is marked complete.</p>
          <p>Current readiness: {canCreate ? "ready" : "waiting for analysis or repo mapping"}.</p>
        </div>

        <Button className="w-full" onClick={handleCreate} disabled={!canCreate || mutation.isPending}>
          {mutation.isPending ? "Creating branch..." : execution?.status === "created" ? "Create branch again" : "Create branch"}
        </Button>
      </CardContent>
    </Card>
  );
}
