import { useAuthStore } from "@/stores/auth";
import type { User } from "@/api/types";

interface UseAuthReturn {
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  isAdmin: boolean;
  isDevOrAbove: boolean;
  setAuth: (token: string, user: User) => void;
  logout: () => void;
}

export function useAuth(): UseAuthReturn {
  const { token, user, setAuth, logout, isAdmin, isDevOrAbove } = useAuthStore();
  return {
    user,
    token,
    isAuthenticated: !!token,
    isAdmin: isAdmin(),
    isDevOrAbove: isDevOrAbove(),
    setAuth,
    logout,
  };
}
