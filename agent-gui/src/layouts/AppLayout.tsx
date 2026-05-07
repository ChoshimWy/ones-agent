import { NavLink, useLocation, Outlet } from "react-router-dom";
import {
  Bug,
  ListTodo,
  ShieldCheck,
  Clock,
  Settings,
  ScrollText,
  ChevronLeft,
  ChevronRight,
  LogOut,
  Menu,
} from "lucide-react";
import { Component, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import {
  Sheet,
  SheetContent,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useUIStore } from "@/stores/ui";
import { useAuth } from "@/hooks/useAuth";
import { cn } from "@/lib/utils";
import ThemeToggle from "./ThemeToggle";

const NAV_ITEMS = [
  { to: "/defects", label: "Defects", icon: Bug },
  { to: "/tasks", label: "Tasks", icon: ListTodo },
  { to: "/approval", label: "Approval", icon: ShieldCheck },
  { to: "/scheduled", label: "History", icon: Clock },
  { to: "/config", label: "Config", icon: Settings, role: "admin" as const },
  { to: "/logs", label: "Logs", icon: ScrollText },
];

function SidebarNav({
  collapsed,
  onNavigate,
}: {
  collapsed: boolean;
  onNavigate?: () => void;
}) {
  const { isAdmin } = useAuth();
  const location = useLocation();

  return (
    <nav className="flex flex-col gap-1 px-2 py-4">
      {NAV_ITEMS.filter(
        (item) => !item.role || (item.role === "admin" && isAdmin)
      ).map((item) => {
        const Icon = item.icon;
        const isActive =
          location.pathname === item.to ||
          (item.to !== "/" && location.pathname.startsWith(item.to));
        const link = (
          <NavLink
            to={item.to}
            onClick={onNavigate}
            className={cn(
              "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
              "hover:bg-accent hover:text-accent-foreground",
              isActive && "bg-accent text-accent-foreground",
              collapsed && "justify-center px-2"
            )}
          >
            <Icon className="h-4 w-4 shrink-0" />
            {!collapsed && <span>{item.label}</span>}
          </NavLink>
        );

        if (collapsed) {
          return (
            <Tooltip key={item.to}>
              <TooltipTrigger render={link} />
              <TooltipContent side="right">{item.label}</TooltipContent>
            </Tooltip>
          );
        }
        return <div key={item.to}>{link}</div>;
      })}
    </nav>
  );
}

function UserMenu() {
  const { user, logout } = useAuth();
  if (!user) return null;

  const initials = user.name
    .split(" ")
    .map((n) => n[0])
    .join("")
    .toUpperCase()
    .slice(0, 2);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={
        <Button variant="ghost" className="w-full justify-start gap-2 px-2">
          <Avatar className="h-7 w-7">
            <AvatarFallback className="text-xs">{initials}</AvatarFallback>
          </Avatar>
          <span className="truncate text-sm">{user.name}</span>
          <Badge variant="secondary" className="ml-auto text-[10px] px-1.5 py-0">
            {user.role}
          </Badge>
        </Button>
      } />
      <DropdownMenuContent align="end" className="w-48">
        <DropdownMenuItem onClick={logout}>
          <LogOut className="mr-2 h-4 w-4" />
          Logout
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

interface EBState {
  hasError: boolean;
  error?: Error;
}

class ErrorBoundary extends Component<{ children: ReactNode }, EBState> {
  state: EBState = { hasError: false };

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex flex-col items-center justify-center gap-4 py-20 text-center">
          <h2 className="text-lg font-semibold">Something went wrong</h2>
          <p className="text-sm text-muted-foreground">
            {this.state.error?.message}
          </p>
          <Button
            onClick={() => this.setState({ hasError: false })}
            variant="outline"
          >
            Try again
          </Button>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function AppLayout() {
  const { sidebarCollapsed, setSidebarCollapsed, sidebarOpen, setSidebarOpen } =
    useUIStore();

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      {/* Desktop sidebar */}
      <aside
        className={cn(
          "hidden md:flex flex-col border-r bg-sidebar-background transition-all duration-200",
          sidebarCollapsed ? "w-16" : "w-56"
        )}
      >
        <div
          className={cn(
            "flex h-14 items-center border-b px-4",
            sidebarCollapsed && "justify-center px-2"
          )}
        >
          {!sidebarCollapsed && (
            <span className="font-semibold text-sm tracking-tight">
              ONES Agent
            </span>
          )}
          <Button
            variant="ghost"
            size="icon"
            className={cn("h-7 w-7", !sidebarCollapsed && "ml-auto")}
            onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
          >
            {sidebarCollapsed ? (
              <ChevronRight className="h-4 w-4" />
            ) : (
              <ChevronLeft className="h-4 w-4" />
            )}
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto">
          <SidebarNav collapsed={sidebarCollapsed} />
        </div>
        <Separator />
        <div className={cn("p-2", sidebarCollapsed && "flex justify-center")}>
          {!sidebarCollapsed ? <UserMenu /> : <ThemeToggle />}
        </div>
      </aside>

      {/* Mobile sidebar */}
      <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
        <SheetContent side="left" className="w-56 p-0">
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <div className="flex h-14 items-center border-b px-4">
            <span className="font-semibold text-sm tracking-tight">
              ONES Agent
            </span>
          </div>
          <SidebarNav
            collapsed={false}
            onNavigate={() => setSidebarOpen(false)}
          />
          <Separator />
          <div className="p-2">
            <UserMenu />
          </div>
        </SheetContent>
      </Sheet>

      {/* Main content */}
      <div className="flex flex-1 flex-col overflow-hidden">
        <header className="flex h-14 items-center gap-4 border-b px-4">
          <Button
            variant="ghost"
            size="icon"
            className="md:hidden"
            onClick={() => setSidebarOpen(true)}
          >
            <Menu className="h-5 w-5" />
          </Button>
          <div className="ml-auto flex items-center gap-2">
            <ThemeToggle />
          </div>
        </header>
        <main className="flex-1 overflow-y-auto p-4 lg:p-6">
          <ErrorBoundary>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
    </div>
  );
}
