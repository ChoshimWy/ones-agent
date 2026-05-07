import type { ReactNode } from "react";
import { useAuth } from "@/hooks/useAuth";

type Role = "admin" | "dev" | "viewer";

const ROLE_HIERARCHY: Record<Role, number> = {
  viewer: 0,
  dev: 1,
  admin: 2,
};

interface ProtectedActionProps {
  role: Role;
  children: ReactNode;
  fallback?: ReactNode;
}

export default function ProtectedAction({ role, children, fallback = null }: ProtectedActionProps) {
  const { user } = useAuth();
  const userRole = (user?.role ?? "viewer") as Role;
  if (ROLE_HIERARCHY[userRole] >= ROLE_HIERARCHY[role]) {
    return <>{children}</>;
  }
  return <>{fallback}</>;
}
