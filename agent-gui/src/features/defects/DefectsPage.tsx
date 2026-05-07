import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Bug, Filter, Search } from "lucide-react";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useDefects, useOnesProjects, useOnesTeamMembers, useProjectRepos } from "@/api/queries";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { DefectAnalysisResult, DefectRecord, WorkItem } from "@/api/types";
import { formatRelativeTime } from "@/utils/format";
import { getAnalysisMeta, getMappingMeta, getWorkflowMeta } from "@/features/defects/defect-meta";

const WORKFLOW_OPTIONS: Array<WorkItem["status"]> = [
  "PENDING",
  "PARSING",
  "PLANNING",
  "WAITING_APPROVAL",
  "CODING",
  "TESTING",
  "PUSHING",
  "REPORTING",
  "SUCCESS",
  "FAILED",
];

const ANALYSIS_OPTIONS: Array<DefectAnalysisResult["status"]> = ["pending", "analyzing", "analyzed", "blocked", "failed"];

const MAPPING_OPTIONS: Array<DefectRecord["mappingStatus"]> = ["mapped", "partial", "missing"];

const PAGE_SIZE = 12;

export default function DefectsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [search, setSearch] = useState(searchParams.get("search") || "");
  const debouncedSearch = useDebouncedValue(search, 250);

  const projectId = searchParams.get("projectId") ?? "";
  const assignee = searchParams.get("assignee") ?? "";
  const status = searchParams.get("status") ?? "";
  const analysisStatus = searchParams.get("analysisStatus") ?? "";
  const mappingStatus = searchParams.get("mappingStatus") ?? "";
  const page = Number(searchParams.get("page") ?? "1");

  const { data, isLoading, error } = useDefects({
    projectId: projectId || undefined,
    assignee: assignee || undefined,
    status: status ? (status as WorkItem["status"]) : undefined,
    analysisStatus: analysisStatus ? (analysisStatus as DefectAnalysisResult["status"]) : undefined,
    mappingStatus: mappingStatus ? (mappingStatus as DefectRecord["mappingStatus"]) : undefined,
    search: debouncedSearch || undefined,
    page,
    pageSize: PAGE_SIZE,
  });
  const { data: onesProjects } = useOnesProjects();
  const { data: teamMembers } = useOnesTeamMembers();
  const { data: projectMappings } = useProjectRepos(projectId || undefined);

  const selectedProject = useMemo(
    () => onesProjects?.find((project) => project.id === projectId) ?? null,
    [onesProjects, projectId]
  );
  const selectedMapping = useMemo(
    () => projectMappings?.find((mapping) => mapping.projectId === projectId) ?? null,
    [projectMappings, projectId]
  );

  function setFilter(key: string, value: string) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "page") next.delete("page");
    setSearchParams(next, { replace: true });
  }

  function clearFilters() {
    setSearch("");
    setSearchParams(new URLSearchParams(), { replace: true });
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-sm uppercase tracking-[0.28em] text-muted-foreground">
            <Bug className="h-4 w-4" /> Defect analysis workspace
          </div>
          <h1 className="text-2xl font-bold tracking-tight">Defects</h1>
          <p className="max-w-2xl text-sm text-muted-foreground">
            Browse ONES defects by project, inspect mapping and analysis status, and open the branch execution boundary from the defect itself.
          </p>
        </div>
        <Badge variant="secondary" className="w-fit">
          {data?.total ?? "—"} defects
        </Badge>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Filters</CardTitle>
          <CardDescription>Use ONES project scope, lifecycle status, and analysis state to narrow the defect queue.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
          <div className="space-y-1.5 xl:col-span-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                value={search}
                onChange={(event) => {
                  setSearch(event.target.value);
                  setFilter("search", event.target.value);
                }}
                placeholder="Search defects, ONES IDs, or root cause text"
                className="pl-9"
              />
            </div>
          </div>

          <Select value={assignee || "all-assignees"} onValueChange={(value) => setFilter("assignee", value === "all-assignees" ? "" : value ?? "") }>
            <SelectTrigger className="w-full">
              <SelectValue placeholder="Assignee" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all-assignees">All assignees</SelectItem>
              {teamMembers?.map((member) => (
                <SelectItem key={member.id} value={member.id}>
                  {member.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={projectId || "all-projects"} onValueChange={(value) => setFilter("projectId", value === "all-projects" ? "" : value ?? "") }>
            <SelectTrigger className="w-full">
              <SelectValue placeholder="Project" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all-projects">All projects</SelectItem>
              {onesProjects?.map((project) => (
                <SelectItem key={project.id} value={project.id}>
                  {project.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={status || "all-statuses"} onValueChange={(value) => setFilter("status", value === "all-statuses" ? "" : value ?? "") }>
            <SelectTrigger className="w-full">
              <SelectValue placeholder="Lifecycle" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all-statuses">All stages</SelectItem>
              {WORKFLOW_OPTIONS.map((item) => (
                <SelectItem key={item} value={item}>
                  {getWorkflowMeta(item).label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={analysisStatus || "all-analysis"} onValueChange={(value) => setFilter("analysisStatus", value === "all-analysis" ? "" : value ?? "") }>
            <SelectTrigger className="w-full">
              <SelectValue placeholder="Analysis" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all-analysis">All analysis states</SelectItem>
              {ANALYSIS_OPTIONS.map((item) => (
                <SelectItem key={item} value={item}>
                  {getAnalysisMeta(item).label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={mappingStatus || "all-mapping"} onValueChange={(value) => setFilter("mappingStatus", value === "all-mapping" ? "" : value ?? "") }>
            <SelectTrigger className="w-full">
              <SelectValue placeholder="Mapping" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all-mapping">All mapping states</SelectItem>
              {MAPPING_OPTIONS.map((item) => (
                <SelectItem key={item} value={item}>
                  {getMappingMeta(item).label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <div className="flex items-center gap-2 xl:col-span-4 xl:justify-end">
            {selectedProject ? <Badge variant="outline">Project: {selectedProject.name}</Badge> : null}
            {selectedMapping?.iterationName ? <Badge variant="outline">Iteration: {selectedMapping.iterationName}</Badge> : null}
            <Button variant="outline" size="sm" onClick={clearFilters} className="gap-2">
              <Filter className="h-4 w-4" /> Reset
            </Button>
          </div>
        </CardContent>
      </Card>

      {error ? (
        <Card>
          <CardContent className="py-10 text-center text-sm text-destructive">
            {error instanceof Error ? error.message : "Unable to load defects"}
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Defect queue</CardTitle>
          <CardDescription>Each row opens a defect detail page with analysis and execution controls.</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          <div className="overflow-hidden rounded-b-xl">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Defect</TableHead>
                  <TableHead>Project</TableHead>
                  <TableHead>Workflow</TableHead>
                  <TableHead>Mapping</TableHead>
                  <TableHead>Analysis</TableHead>
                  <TableHead>Updated</TableHead>
                  <TableHead className="text-right">Open</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {isLoading ? (
                  Array.from({ length: 5 }).map((_, row) => (
                    <TableRow key={row}>
                      {Array.from({ length: 7 }).map((__, column) => (
                        <TableCell key={column}>
                          <Skeleton className="h-5 w-full" />
                        </TableCell>
                      ))}
                    </TableRow>
                  ))
                ) : data?.items.length ? (
                  data.items.map((defect) => {
                    const workflowMeta = getWorkflowMeta(defect.status);
                    const mappingMeta = getMappingMeta(defect.mappingStatus);
                    const analysisMeta = getAnalysisMeta(defect.analysisStatus);

                    return (
                      <TableRow key={defect.id}>
                        <TableCell className="max-w-[280px] align-top">
                          <div className="space-y-1">
                            <Badge variant="outline" className="w-fit font-mono text-[10px]">
                              {defect.onesId}
                            </Badge>
                            <Link to={`/defects/${defect.id}`} className="block font-medium leading-5 hover:text-primary">
                              {defect.title}
                            </Link>
                            <p className="line-clamp-2 text-xs text-muted-foreground">
                              {defect.analysisSummary || defect.rootCause || "Analysis pending"}
                            </p>
                          </div>
                        </TableCell>
                        <TableCell className="align-top text-sm">{defect.projectName}</TableCell>
                        <TableCell className="align-top">
                          <Badge variant={workflowMeta.variant}>{workflowMeta.label}</Badge>
                        </TableCell>
                        <TableCell className="align-top">
                          <Badge variant={mappingMeta.variant}>{mappingMeta.label}</Badge>
                        </TableCell>
                        <TableCell className="align-top">
                          <Badge variant={analysisMeta.variant}>{analysisMeta.label}</Badge>
                        </TableCell>
                        <TableCell className="align-top text-xs text-muted-foreground">
                          {formatRelativeTime(defect.updatedAt)}
                        </TableCell>
                        <TableCell className="align-top text-right">
                          <Link
                            to={`/defects/${defect.id}`}
                            className="inline-flex h-8 items-center justify-center rounded-lg border border-input bg-background px-3 text-sm font-medium text-foreground transition-colors hover:bg-muted"
                          >
                            Open
                          </Link>
                        </TableCell>
                      </TableRow>
                    );
                  })
                ) : (
                  <TableRow>
                    <TableCell colSpan={7} className="py-12 text-center text-sm text-muted-foreground">
                      No defects matched the current filters.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      {data && data.total > PAGE_SIZE ? (
        <div className="flex items-center justify-center gap-2">
          <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setFilter("page", String(page - 1))}>
            Previous
          </Button>
          <span className="text-sm text-muted-foreground">
            Page {page} of {Math.max(1, Math.ceil(data.total / PAGE_SIZE))}
          </span>
          <Button variant="outline" size="sm" disabled={page * PAGE_SIZE >= data.total} onClick={() => setFilter("page", String(page + 1))}>
            Next
          </Button>
        </div>
      ) : null}
    </div>
  );
}
