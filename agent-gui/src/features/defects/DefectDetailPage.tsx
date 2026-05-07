import { Link, useParams } from "react-router-dom";
import { ArrowLeft, FileText, Layers3, Sparkles } from "lucide-react";
import { useMemo } from "react";
import { useDefectDetail } from "@/api/queries";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { formatRelativeTime } from "@/utils/format";
import { getAnalysisMeta, getExecutionMeta, getMappingMeta, getWorkflowMeta, formatMaybe } from "@/features/defects/defect-meta";
import MarkdownRenderer from "@/features/defects/MarkdownRenderer";
import DefectExecutionPanel from "@/features/defects/DefectExecutionPanel";

export default function DefectDetailPage() {
  const params = useParams();
  const defectId = params.defectId ?? params.id ?? "";
  const { data: defect, isLoading, error } = useDefectDetail(defectId);

  const analysis = defect?.analysis;
  const analysisStatus = analysis?.status ?? defect?.analysisStatus ?? "pending";
  const analysisMeta = getAnalysisMeta(analysisStatus);
  const mappingMeta = getMappingMeta(defect?.mappingStatus ?? "partial");
  const workflowMeta = getWorkflowMeta(defect?.status ?? "PENDING");
  const executionMeta = getExecutionMeta(defect?.execution?.status ?? defect?.executionStatus ?? "idle");

  const evidence = useMemo(() => analysis?.evidence ?? defect?.analysisEvidence ?? [], [analysis, defect]);
  const markdown = analysis?.markdown ?? defect?.analysisMarkdown ?? "";
  const fixSuggestions = analysis?.fixSuggestions ?? defect?.fixSuggestions ?? [];
  const rootCause = analysis?.rootCause ?? defect?.rootCause ?? "Analysis pending.";
  const summary = analysis?.summary ?? defect?.analysisSummary ?? "No summary available yet.";

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-6 w-40" />
        <Skeleton className="h-12 w-full" />
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.05fr)_minmax(320px,0.95fr)]">
          <Skeleton className="h-72 w-full" />
          <Skeleton className="h-72 w-full" />
        </div>
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }

  if (error || !defect) {
    return (
      <Card>
        <CardContent className="space-y-3 py-12 text-center">
          <p className="text-sm font-medium">Defect not found</p>
          <p className="text-sm text-muted-foreground">
            {error instanceof Error ? error.message : "The selected defect could not be loaded."}
          </p>
          <Link
            to="/defects"
            className="inline-flex h-9 items-center justify-center rounded-lg border border-input bg-background px-3 text-sm font-medium text-foreground transition-colors hover:bg-muted"
          >
            Back to defects
          </Link>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-3">
          <Link to="/defects" className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground">
            <ArrowLeft className="h-4 w-4" /> Back to defects
          </Link>
          <div className="space-y-2">
            <p className="text-xs uppercase tracking-[0.28em] text-muted-foreground">Normalized defect detail</p>
            <h1 className="text-2xl font-bold tracking-tight">{defect.title}</h1>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline" className="font-mono text-[10px]">{defect.onesId}</Badge>
              <Badge variant={workflowMeta.variant}>{workflowMeta.label}</Badge>
              <Badge variant={mappingMeta.variant}>{mappingMeta.label}</Badge>
              <Badge variant={analysisMeta.variant}>{analysisMeta.label}</Badge>
              <Badge variant={executionMeta.variant}>{executionMeta.label}</Badge>
            </div>
          </div>
        </div>
        <div className="hidden lg:flex flex-col items-end gap-2 text-right">
          <Badge variant="secondary">{defect.projectName}</Badge>
          <p className="text-xs text-muted-foreground">Updated {formatRelativeTime(defect.updatedAt)}</p>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.05fr)_minmax(320px,0.95fr)]">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Normalized defect metadata</CardTitle>
            <CardDescription>Canonical ONES defect fields, repo mapping, and current workflow state.</CardDescription>
          </CardHeader>
          <CardContent>
            <dl className="grid gap-4 sm:grid-cols-2">
              <MetaField label="ONES ID" value={defect.onesId} mono />
              <MetaField label="Project" value={defect.projectName} />
              <MetaField label="Assignee" value={formatMaybe(defect.assignee)} />
              <MetaField label="Priority" value={formatMaybe(defect.priority)} />
              <MetaField label="Workflow" value={workflowMeta.label} />
              <MetaField label="Mapping" value={mappingMeta.label} />
              <MetaField label="Analysis" value={analysisMeta.label} />
              <MetaField label="Codebase" value={formatMaybe(defect.codebaseStatus)} />
              <MetaField label="Created" value={formatRelativeTime(defect.createdAt)} />
              <MetaField label="Updated" value={formatRelativeTime(defect.updatedAt)} />
              <MetaField label="Iteration" value={formatMaybe(defect.selectedRepo?.iterationName ?? defect.selectedRepo?.iterationId)} />
              <MetaField label="Base branch" value={formatMaybe(defect.execution?.baseBranch ?? defect.baseBranch ?? defect.selectedRepo?.branch)} mono />
              <MetaField label="Repo" value={formatMaybe(defect.selectedRepo?.repoUrl)} mono />
            </dl>
          </CardContent>
        </Card>

        <DefectExecutionPanel defect={defect} />
      </div>

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">
            <Sparkles className="h-4 w-4" /> Overview
          </TabsTrigger>
          <TabsTrigger value="evidence">
            <Layers3 className="h-4 w-4" /> Evidence
          </TabsTrigger>
          <TabsTrigger value="markdown">
            <FileText className="h-4 w-4" /> Markdown
          </TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="mt-4 space-y-4">
          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Root cause</CardTitle>
                <CardDescription>{summary}</CardDescription>
              </CardHeader>
              <CardContent>
                <p className="leading-7 text-foreground">{rootCause}</p>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="text-base">Fix suggestions</CardTitle>
                <CardDescription>Actionable changes derived from the current analysis result.</CardDescription>
              </CardHeader>
              <CardContent>
                {fixSuggestions.length > 0 ? (
                  <ol className="space-y-3 list-decimal pl-5 text-sm text-foreground">
                    {fixSuggestions.map((item) => (
                      <li key={item} className="leading-7">{item}</li>
                    ))}
                  </ol>
                ) : (
                  <p className="text-sm text-muted-foreground">No fix suggestions available yet.</p>
                )}
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="evidence" className="mt-4">
          {evidence.length > 0 ? (
            <div className="grid gap-4 lg:grid-cols-2">
              {evidence.map((item) => (
                <Card key={item.id}>
                  <CardHeader>
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <CardTitle className="text-base">{item.label}</CardTitle>
                        <CardDescription>{item.source}</CardDescription>
                      </div>
                      <Badge variant="outline">{item.kind}</Badge>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-3 text-sm">
                    <p className="leading-7 text-foreground">{item.label}</p>
                    <p className="text-muted-foreground">{item.source}</p>
                    <p className="leading-7 text-muted-foreground">{item.snippet || item.summary}</p>
                    {item.path ? <p className="font-mono text-xs text-muted-foreground">{item.path}</p> : null}
                  </CardContent>
                </Card>
              ))}
            </div>
          ) : (
            <Card>
              <CardContent className="py-10 text-center text-sm text-muted-foreground">
                No structured evidence is available yet.
              </CardContent>
            </Card>
          )}
        </TabsContent>

        <TabsContent value="markdown" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Rendered analysis markdown</CardTitle>
              <CardDescription>Human-readable output rendered from the same structured analysis result.</CardDescription>
            </CardHeader>
            <CardContent>
              <MarkdownRenderer markdown={markdown} />
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function MetaField({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="space-y-1">
      <dt className="text-xs uppercase tracking-[0.2em] text-muted-foreground">{label}</dt>
      <dd className={mono ? "font-mono text-xs break-all text-foreground" : "text-sm text-foreground"}>{value}</dd>
    </div>
  );
}
