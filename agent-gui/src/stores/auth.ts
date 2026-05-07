import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { User } from "@/api/types";

interface AuthState {
  token: string | null;
  user: User | null;
  setAuth: (token: string, user: User) => void;
  logout: () => void;
  isAdmin: () => boolean;
  isDevOrAbove: () => boolean;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      token: null,
      user: null,
      setAuth: (token, user) => {
        localStorage.setItem("auth_token", token);
        set({ token, user });
      },
      logout: () => {
        localStorage.removeItem("auth_token");
        set({ token: null, user: null });
      },
      isAdmin: () => get().user?.role === "admin",
      isDevOrAbove: () => {
        const role = get().user?.role;
        return role === "admin" || role === "dev";
      },
    }),
    { name: "auth-storage" }
  )
);
