import { useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { useConfig, useUpdateConfig, useTestConnection, useProjectRepos, useAddProjectRepo, useRemoveProjectRepo, useOnesProjectIterations, useOnesProjects } from "@/api/queries";
import type { AppConfig, OnesIteration, ProjectRepo } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Eye, EyeOff, Loader2, CheckCircle2, XCircle } from "lucide-react";
import { maskSecret } from "@/utils/format";

const configSchema = z.object({
  ones: z.object({
    baseUrl: z.string(),
    email: z.string(),
    password: z.string(),
    teamId: z.string(),
    projectId: z.string(),
  }),
  git: z.object({
    repoUrl: z.string(),
    branch: z.string(),
    authType: z.enum(["https", "ssh"]),
  }),
  llm: z.object({
    provider: z.string(),
    model: z.string(),
    baseUrl: z.string(),
    apiKey: z.string(),
  }),
  cicd: z.object({
    platform: z.enum(["github", "gitlab", "none"]),
    token: z.string().optional(),
  }),
  webhook: z.object({
    secret: z.string().optional(),
    enabled: z.boolean(),
  }),
  email: z.object({
    smtpHost: z.string(),
    smtpPort: z.number(),
    smtpUser: z.string(),
    smtpPassword: z.string(),
    sender: z.string(),
    useTls: z.boolean(),
  }).optional(),
});

type ConfigFormValues = z.infer<typeof configSchema>;

function SecretInput({ label, id, value, onChange }: {
  label: string;
  id: string;
  value: string;
  onChange: (v: string) => void;
}) {
  const [visible, setVisible] = useState(false);

  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      <div className="relative">
        <Input
          id={id}
          type={visible ? "text" : "password"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="pr-10"
        />
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="absolute right-0 top-0 h-full px-3"
          onClick={() => setVisible(!visible)}
        >
          {visible ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
        </Button>
      </div>
      {!visible && value && (
        <p className="text-xs text-muted-foreground">{maskSecret(value)}</p>
      )}
    </div>
  );
}

function TestButton({ section }: { section: keyof AppConfig }) {
  const mutation = useTestConnection();
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);

  function handleTest() {
    mutation.mutate(section, {
      onSuccess: (data) => setResult(data),
    });
  }

  return (
    <div className="flex items-center gap-2">
      <Button type="button" variant="outline" size="sm" onClick={handleTest} disabled={mutation.isPending}>
        {mutation.isPending && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
        Test Connection
      </Button>
      {result && (
        <Badge variant={result.ok ? "default" : "destructive"} className="gap-1">
          {result.ok ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
          {result.message}
        </Badge>
      )}
    </div>
  );
}

export default function ConfigPage() {
  const { data: config, isLoading } = useConfig();
  const updateMutation = useUpdateConfig();

  const {
    register,
    handleSubmit,
    formState: { errors, isDirty },
    watch,
    setValue,
    reset,
  } = useForm<ConfigFormValues>({
    resolver: zodResolver(configSchema),
    defaultValues: {
      ones: { baseUrl: "", email: "", password: "", teamId: "", projectId: "" },
      git: { repoUrl: "", branch: "main", authType: "https" },
      llm: { provider: "openai", model: "", baseUrl: "", apiKey: "" },
      cicd: { platform: "github", token: "" },
      webhook: { secret: "", enabled: true },
      email: { smtpHost: "", smtpPort: 465, smtpUser: "", smtpPassword: "", sender: "", useTls: true },
    },
  });

  const initialized = useRef(false);

  useEffect(() => {
    if (config && !initialized.current) {
      reset(config);
      initialized.current = true;
    }
  }, [config, reset]);

  function onSubmit(values: ConfigFormValues) {
    updateMutation.mutate(values, {
      onSuccess: () => {
        reset(values);
        initialized.current = true;
      },
    });
  }

  function onInvalid(err: typeof errors) {
    const messages: string[] = [];
    for (const [key, val] of Object.entries(err)) {
      if (val && typeof val === "object" && "message" in val) {
        messages.push(`${key}: ${(val as { message: string }).message}`);
      } else if (val && typeof val === "object") {
        for (const [subKey, subVal] of Object.entries(val as Record<string, unknown>)) {
          if (subVal && typeof subVal === "object" && "message" in (subVal as object)) {
            messages.push(`${key}.${subKey}: ${(subVal as { message: string }).message}`);
          }
        }
      }
    }
    const desc = messages.length > 0 ? messages.join("; ") : "Validation failed";
    import("sonner").then(({ toast }) => {
      toast.error("Validation Error", { description: desc });
    });
  }

  if (isLoading) return <div className="flex justify-center py-20"><Loader2 className="h-6 w-6 animate-spin" /></div>;

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold tracking-tight">Configuration</h1>

      <form onSubmit={handleSubmit(onSubmit, onInvalid)} className="space-y-4">
        <Tabs defaultValue="ones">
          <TabsList>
            <TabsTrigger value="ones">ONES</TabsTrigger>
            <TabsTrigger value="projects">Projects</TabsTrigger>
            <TabsTrigger value="git">Git</TabsTrigger>
            <TabsTrigger value="llm">LLM</TabsTrigger>
            <TabsTrigger value="cicd">CI/CD</TabsTrigger>
            <TabsTrigger value="webhook">Webhook</TabsTrigger>
            <TabsTrigger value="email">Email</TabsTrigger>
          </TabsList>

          <TabsContent value="ones" className="mt-4">
            <Card>
              <CardHeader className="flex flex-row items-center justify-between">
                <CardTitle className="text-base">ONES Integration</CardTitle>
                <TestButton section="ones" />
              </CardHeader>
              <CardContent className="grid gap-4 md:grid-cols-2">
                <div className="space-y-1">
                  <Label htmlFor="ones.baseUrl">Base URL</Label>
                  <Input id="ones.baseUrl" {...register("ones.baseUrl")} />
                  {errors.ones?.baseUrl && <p className="text-xs text-destructive">{errors.ones.baseUrl.message}</p>}
                </div>
                <div className="space-y-1">
                  <Label htmlFor="ones.email">Email</Label>
                  <Input id="ones.email" type="email" {...register("ones.email")} />
                  {errors.ones?.email && <p className="text-xs text-destructive">{errors.ones.email.message}</p>}
                </div>
                <SecretInput
                  label="Password"
                  id="ones.password"
                  value={watch("ones.password") ?? ""}
                  onChange={(v) => setValue("ones.password", v, { shouldDirty: true })}
                />
                <div className="space-y-1">
                  <Label htmlFor="ones.teamId">Team ID</Label>
                  <Input id="ones.teamId" {...register("ones.teamId")} />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="ones.projectId">Project ID</Label>
                  <Input id="ones.projectId" {...register("ones.projectId")} />
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="projects" className="mt-4">
            <ProjectReposTab />
          </TabsContent>

          <TabsContent value="git" className="mt-4">
            <Card>
              <CardHeader className="flex flex-row items-center justify-between">
                <CardTitle className="text-base">Git Repository</CardTitle>
                <TestButton section="git" />
              </CardHeader>
              <CardContent className="grid gap-4 md:grid-cols-2">
                <div className="space-y-1">
                  <Label htmlFor="git.repoUrl">Repository URL</Label>
                  <Input id="git.repoUrl" {...register("git.repoUrl")} />
                  {errors.git?.repoUrl && <p className="text-xs text-destructive">{errors.git.repoUrl.message}</p>}
                </div>
                <div className="space-y-1">
                  <Label htmlFor="git.branch">Branch</Label>
                  <Input id="git.branch" {...register("git.branch")} />
                </div>
                <div className="space-y-1">
                  <Label>Auth Type</Label>
                  <div className="flex gap-2">
                    <Button
                      type="button"
                      variant={watch("git.authType") === "https" ? "default" : "outline"}
                      size="sm"
                      onClick={() => setValue("git.authType", "https", { shouldDirty: true })}
                    >
                      HTTPS
                    </Button>
                    <Button
                      type="button"
                      variant={watch("git.authType") === "ssh" ? "default" : "outline"}
                      size="sm"
                      onClick={() => setValue("git.authType", "ssh", { shouldDirty: true })}
                    >
                      SSH
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="llm" className="mt-4">
            <Card>
              <CardHeader className="flex flex-row items-center justify-between">
                <CardTitle className="text-base">LLM Engine</CardTitle>
                <TestButton section="llm" />
              </CardHeader>
              <CardContent className="grid gap-4 md:grid-cols-2">
                <div className="space-y-1">
                  <Label htmlFor="llm.provider">Provider</Label>
                  <Input id="llm.provider" {...register("llm.provider")} />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="llm.model">Model</Label>
                  <Input id="llm.model" {...register("llm.model")} />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="llm.baseUrl">Base URL</Label>
                  <Input id="llm.baseUrl" {...register("llm.baseUrl")} />
                  {errors.llm?.baseUrl && <p className="text-xs text-destructive">{errors.llm.baseUrl.message}</p>}
                </div>
                <SecretInput
                  label="API Key"
                  id="llm.apiKey"
                  value={watch("llm.apiKey") ?? ""}
                  onChange={(v) => setValue("llm.apiKey", v, { shouldDirty: true })}
                />
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="cicd" className="mt-4">
            <Card>
              <CardHeader className="flex flex-row items-center justify-between">
                <CardTitle className="text-base">CI/CD</CardTitle>
                <TestButton section="cicd" />
              </CardHeader>
              <CardContent className="grid gap-4 md:grid-cols-2">
                <div className="space-y-1">
                  <Label>Platform</Label>
                  <div className="flex gap-2">
                    {(["github", "gitlab", "none"] as const).map((p) => (
                      <Button
                        key={p}
                        type="button"
                        variant={watch("cicd.platform") === p ? "default" : "outline"}
                        size="sm"
                        onClick={() => setValue("cicd.platform", p, { shouldDirty: true })}
                      >
                        {p}
                      </Button>
                    ))}
                  </div>
                </div>
                <SecretInput
                  label="API Token"
                  id="cicd.token"
                  value={watch("cicd.token") ?? ""}
                  onChange={(v) => setValue("cicd.token", v, { shouldDirty: true })}
                />
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="webhook" className="mt-4">
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Webhook</CardTitle>
              </CardHeader>
              <CardContent className="grid gap-4 md:grid-cols-2">
                <SecretInput
                  label="Secret"
                  id="webhook.secret"
                  value={watch("webhook.secret") ?? ""}
                  onChange={(v) => setValue("webhook.secret", v, { shouldDirty: true })}
                />
                <div className="space-y-1">
                  <Label>Enabled</Label>
                  <div className="flex gap-2">
                    <Button
                      type="button"
                      variant={watch("webhook.enabled") ? "default" : "outline"}
                      size="sm"
                      onClick={() => setValue("webhook.enabled", true, { shouldDirty: true })}
                    >
                      Enabled
                    </Button>
                    <Button
                      type="button"
                      variant={!watch("webhook.enabled") ? "default" : "outline"}
                      size="sm"
                      onClick={() => setValue("webhook.enabled", false, { shouldDirty: true })}
                    >
                      Disabled
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="email" className="mt-4">
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Email SMTP</CardTitle>
              </CardHeader>
              <CardContent className="grid gap-4 md:grid-cols-2">
                <div className="space-y-1">
                  <Label htmlFor="email.smtpHost">SMTP Host</Label>
                  <Input id="email.smtpHost" {...register("email.smtpHost")} />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="email.smtpPort">SMTP Port</Label>
                  <Input id="email.smtpPort" type="number" {...register("email.smtpPort", { valueAsNumber: true })} />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="email.smtpUser">SMTP User</Label>
                  <Input id="email.smtpUser" {...register("email.smtpUser")} />
                </div>
                <SecretInput
                  label="SMTP Password"
                  id="email.smtpPassword"
                  value={watch("email.smtpPassword") ?? ""}
                  onChange={(v) => setValue("email.smtpPassword", v, { shouldDirty: true })}
                />
                <div className="space-y-1">
                  <Label htmlFor="email.sender">Sender Email</Label>
                  <Input id="email.sender" {...register("email.sender")} />
                </div>
                <div className="space-y-1">
                  <Label>Use TLS</Label>
                  <div className="flex gap-2">
                    <Button
                      type="button"
                      variant={watch("email.useTls") ? "default" : "outline"}
                      size="sm"
                      onClick={() => setValue("email.useTls", true, { shouldDirty: true })}
                    >
                      Enabled
                    </Button>
                    <Button
                      type="button"
                      variant={!watch("email.useTls") ? "default" : "outline"}
                      size="sm"
                      onClick={() => setValue("email.useTls", false, { shouldDirty: true })}
                    >
                      Disabled
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>

        <Separator />

        <div className="flex items-center gap-2">
          <Button type="submit" disabled={updateMutation.isPending}>
            {updateMutation.isPending && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
            Save Configuration
          </Button>
          {isDirty && (
            <Button type="button" variant="outline" onClick={() => reset(config)}>
              Discard Changes
            </Button>
          )}
          {updateMutation.isSuccess && (
            <Badge variant="default" className="gap-1">
              <CheckCircle2 className="h-3 w-3" /> Saved
            </Badge>
          )}
          {updateMutation.isError && (
            <Badge variant="destructive" className="gap-1">
              <XCircle className="h-3 w-3" /> {updateMutation.error?.message || "Save failed"}
            </Badge>
          )}
        </div>
      </form>
    </div>
  );
}

function ProjectReposTab() {
  const { data: mappings, isLoading } = useProjectRepos();
  const { data: onesProjects, isLoading: projectsLoading, error: projectsError } = useOnesProjects();
  const [form, setForm] = useState({
    projectId: "",
    projectName: "",
    repoUrl: "",
    branch: "main",
    iterationId: "",
    iterationName: "",
    iterationKey: "",
  });
  const {
    data: iterations,
    isLoading: iterationsLoading,
    error: iterationsError,
  } = useOnesProjectIterations(form.projectId || undefined);
  const addMutation = useAddProjectRepo();
  const removeMutation = useRemoveProjectRepo();

  function handleProjectSelect(e: React.ChangeEvent<HTMLSelectElement>) {
    const id = e.target.value;
    const proj = onesProjects?.find((p) => p.id === id);
    setForm((f) => ({
      ...f,
      projectId: id,
      projectName: proj?.name || f.projectName,
      iterationId: "",
      iterationName: "",
      iterationKey: "",
    }));
  }

  function handleIterationSelect(e: React.ChangeEvent<HTMLSelectElement>) {
    const id = e.target.value;
    const iteration = iterations?.find((item) => item.id === id);
    setForm((f) => ({
      ...f,
      iterationId: id,
      iterationName: iteration?.name || "",
      iterationKey: iteration?.key || "",
    }));
  }

  function handleAdd() {
    if (!form.projectId || !form.repoUrl || !form.iterationId) return;
    addMutation.mutate(form, {
      onSuccess: () => setForm({
        projectId: "",
        projectName: "",
        repoUrl: "",
        branch: "main",
        iterationId: "",
        iterationName: "",
        iterationKey: "",
      }),
    });
  }

  function handleRemove(projectId: string, repoUrl: string) {
    removeMutation.mutate({ projectId, repoUrl });
  }

  if (isLoading) return <div className="flex justify-center py-10"><Loader2 className="h-5 w-5 animate-spin" /></div>;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Project-Repo Mappings</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Map ONES projects to their corresponding Git repositories. When a defect is detected from a project, the mapped repo will be used for code changes.
        </p>

        <div className="grid gap-3 md:grid-cols-5">
          <div className="space-y-1">
            <Label>ONES Project</Label>
            {projectsLoading ? (
              <div className="flex items-center h-9 px-3 text-sm text-muted-foreground"><Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />Loading projects...</div>
            ) : projectsError ? (
              <Input
                placeholder="Enter Project ID manually"
                value={form.projectId}
                onChange={(e) => setForm((f) => ({ ...f, projectId: e.target.value }))}
              />
            ) : (
              <select
                className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                value={form.projectId}
                onChange={handleProjectSelect}
              >
                <option value="">Select a project...</option>
                {onesProjects?.map((p) => (
                  <option key={p.id} value={p.id}>{p.name} ({p.id})</option>
                ))}
              </select>
            )}
          </div>
          <div className="space-y-1">
            <Label>Iteration</Label>
            {!form.projectId ? (
              <Input value="" placeholder="Select a project first" disabled />
            ) : iterationsLoading ? (
              <div className="flex items-center h-9 px-3 text-sm text-muted-foreground"><Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />Loading iterations...</div>
            ) : iterationsError ? (
              <Input value="" placeholder="Unable to load iterations" disabled />
            ) : (
              <select
                className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                value={form.iterationId}
                onChange={handleIterationSelect}
              >
                <option value="">Select an iteration...</option>
                {iterations?.map((iteration: OnesIteration) => (
                  <option key={iteration.id} value={iteration.id}>
                    {iteration.name}{iteration.key ? ` (${iteration.key})` : ""}
                  </option>
                ))}
              </select>
            )}
          </div>
          <div className="space-y-1">
            <Label>Project Name</Label>
            <Input
              placeholder="Auto-filled from selection"
              value={form.projectName}
              onChange={(e) => setForm((f) => ({ ...f, projectName: e.target.value }))}
            />
          </div>
          <div className="space-y-1">
            <Label>Repo URL</Label>
            <Input
              placeholder="e.g. https://gitlab.com/group/repo.git"
              value={form.repoUrl}
              onChange={(e) => setForm((f) => ({ ...f, repoUrl: e.target.value }))}
            />
          </div>
          <div className="space-y-1">
            <Label>Branch</Label>
            <div className="flex gap-2">
                  <Input
                    value={form.branch}
                    onChange={(e) => setForm((f) => ({ ...f, branch: e.target.value }))}
                    className="flex-1"
                  />
                <Button type="button" size="sm" onClick={handleAdd} disabled={!form.projectId || !form.repoUrl || !form.iterationId || addMutation.isPending}>
                  {addMutation.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Add"}
                </Button>
              </div>
            </div>
        </div>

        {form.projectId && form.iterationName ? (
          <p className="text-xs text-muted-foreground">
            Selected iteration: {form.iterationName}{form.iterationKey ? ` (${form.iterationKey})` : ""}
          </p>
        ) : null}

        {mappings && mappings.length > 0 && (
          <div className="rounded-md border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-muted/50">
                  <th className="px-3 py-2 text-left font-medium">Project</th>
                  <th className="px-3 py-2 text-left font-medium">Iteration</th>
                  <th className="px-3 py-2 text-left font-medium">Repo URL</th>
                  <th className="px-3 py-2 text-left font-medium">Branch</th>
                  <th className="px-3 py-2 w-16"></th>
                </tr>
              </thead>
              <tbody>
                {mappings.map((m: ProjectRepo) => (
                  <tr key={`${m.projectId}-${m.repoUrl}`} className="border-b last:border-0">
                    <td className="px-3 py-2">
                      <div className="font-medium">{m.projectName || "—"}</div>
                      <div className="font-mono text-xs text-muted-foreground">{m.projectId}</div>
                    </td>
                    <td className="px-3 py-2">
                      <div className="font-medium">{m.iterationName || "—"}</div>
                      <div className="font-mono text-xs text-muted-foreground">{m.iterationKey || m.iterationId || ""}</div>
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">{m.repoUrl}</td>
                    <td className="px-3 py-2">{m.branch}</td>
                    <td className="px-3 py-2">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-7 text-destructive hover:text-destructive"
                        onClick={() => handleRemove(m.projectId, m.repoUrl)}
                        disabled={removeMutation.isPending}
                      >
                        Remove
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {(!mappings || mappings.length === 0) && (
          <p className="text-sm text-muted-foreground text-center py-6">No project-repo mappings configured yet.</p>
        )}
      </CardContent>
    </Card>
  );
}
