import { lazy, Suspense } from "react";
import { createBrowserRouter, Navigate } from "react-router-dom";
import AppLayout from "@/layouts/AppLayout";
import { useAuthStore } from "@/stores/auth";

const Dashboard = lazy(() => import("@/features/dashboard/Dashboard"));
const TaskBoard = lazy(() => import("@/features/task-board/TaskBoard"));
const DefectsPage = lazy(() => import("@/features/defects/DefectsPage"));
const DefectDetailPage = lazy(() => import("@/features/defects/DefectDetailPage"));
const Approval = lazy(() => import("@/features/approval/Approval"));
const ScheduledTasks = lazy(() => import("@/features/scheduled/ScheduledTasksPage"));
const Config = lazy(() => import("@/features/config/ConfigPage"));
const Logs = lazy(() => import("@/features/logs/LogsPage"));
const Login = lazy(() => import("@/routes/LoginPage"));
const Forbidden = lazy(() => import("@/routes/ForbiddenPage"));
const NotFound = lazy(() => import("@/routes/NotFoundPage"));

function PageLoader() {
  return (
    <div className="flex items-center justify-center h-64">
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
    </div>
  );
}

function SuspenseWrap({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<PageLoader />}>{children}</Suspense>;
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const token = useAuthStore((s) => s.token);
  if (!token) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function RequireAdmin({ children }: { children: React.ReactNode }) {
  const { token, user } = useAuthStore();
  if (!token) return <Navigate to="/login" replace />;
  if (user?.role !== "admin") return <Navigate to="/403" replace />;
  return <>{children}</>;
}

export const router = createBrowserRouter([
  {
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [
      {
        index: true,
        element: <Navigate to="/defects" replace />,
      },
      {
        path: "dashboard",
        element: (
          <SuspenseWrap>
            <Dashboard />
          </SuspenseWrap>
        ),
      },
      {
        path: "tasks",
        element: (
          <SuspenseWrap>
            <TaskBoard />
          </SuspenseWrap>
        ),
      },
      {
        path: "defects",
        element: (
          <SuspenseWrap>
            <DefectsPage />
          </SuspenseWrap>
        ),
      },
      {
        path: "defects/:id",
        element: (
          <SuspenseWrap>
            <DefectDetailPage />
          </SuspenseWrap>
        ),
      },
      {
        path: "approval",
        element: (
          <SuspenseWrap>
            <Approval />
          </SuspenseWrap>
        ),
      },
      {
        path: "scheduled",
        element: (
          <SuspenseWrap>
            <ScheduledTasks />
          </SuspenseWrap>
        ),
      },
      {
        path: "config",
        element: (
          <RequireAdmin>
            <SuspenseWrap>
              <Config />
            </SuspenseWrap>
          </RequireAdmin>
        ),
      },
      {
        path: "logs",
        element: (
          <SuspenseWrap>
            <Logs />
          </SuspenseWrap>
        ),
      },
    ],
  },
  {
    path: "/login",
    element: (
      <SuspenseWrap>
        <Login />
      </SuspenseWrap>
    ),
  },
  {
    path: "/403",
    element: (
      <SuspenseWrap>
        <Forbidden />
      </SuspenseWrap>
    ),
  },
  {
    path: "*",
    element: (
      <SuspenseWrap>
        <NotFound />
      </SuspenseWrap>
    ),
  },
]);
