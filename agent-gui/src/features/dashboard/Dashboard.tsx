import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useMetrics } from "@/api/queries";
import { formatDuration } from "@/utils/format";
import { Activity, CheckCircle, Clock, XCircle } from "lucide-react";

const CARDS = [
  { key: "activeTasks", label: "Active Tasks", icon: Activity, format: (v: number) => String(v) },
  { key: "successRate", label: "Success Rate", icon: CheckCircle, format: (v: number) => `${(v * 100).toFixed(1)}%` },
  { key: "avgDurationSec", label: "Avg Duration", icon: Clock, format: (v: number) => formatDuration(v) },
  { key: "todayFailures", label: "Today Failures", icon: XCircle, format: (v: number) => String(v) },
] as const;

function MetricCard({ label, value, icon: Icon, format }: {
  label: string;
  value: number | undefined;
  icon: React.ComponentType<{ className?: string }>;
  format: (v: number) => string;
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">{label}</CardTitle>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        {value !== undefined ? (
          <div className="text-2xl font-bold">{format(value)}</div>
        ) : (
          <Skeleton className="h-8 w-20" />
        )}
      </CardContent>
    </Card>
  );
}

export default function Dashboard() {
  const { data, isLoading } = useMetrics();

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        {CARDS.map((c) => (
          <MetricCard
            key={c.key}
            label={c.label}
            value={isLoading ? undefined : data?.[c.key] as number}
            icon={c.icon}
            format={c.format}
          />
        ))}
      </div>

      <ThroughputChart data={data?.dailyThroughput} isLoading={isLoading} />

      <LiveFeed />
    </div>
  );
}

function ThroughputChart({ data, isLoading }: { data?: { date: string; count: number; success: number }[]; isLoading: boolean }) {
  if (isLoading) return <Card><CardContent className="h-64"><Skeleton className="h-full w-full" /></CardContent></Card>;
  if (!data) return null;

  const maxCount = Math.max(...data.map((d) => d.count));

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">7-Day Throughput</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex items-end gap-2 h-48">
          {data.map((d) => (
            <div key={d.date} className="flex flex-1 flex-col items-center gap-1">
              <div className="flex w-full gap-0.5 items-end h-40">
                <div
                  className="flex-1 rounded-t bg-primary transition-all"
                  style={{ height: `${(d.count / maxCount) * 100}%` }}
                  title={`Total: ${d.count}`}
                />
                <div
                  className="flex-1 rounded-t bg-primary/30 transition-all"
                  style={{ height: `${(d.success / maxCount) * 100}%` }}
                  title={`Success: ${d.success}`}
                />
              </div>
              <span className="text-[10px] text-muted-foreground">{d.date.slice(5)}</span>
            </div>
          ))}
        </div>
        <div className="flex gap-4 mt-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-1"><span className="h-2 w-2 rounded bg-primary" /> Total</span>
          <span className="flex items-center gap-1"><span className="h-2 w-2 rounded bg-primary/30" /> Success</span>
        </div>
      </CardContent>
    </Card>
  );
}

function LiveFeed() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Live Events</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">Events will appear here in real time via SSE.</p>
      </CardContent>
    </Card>
  );
}
