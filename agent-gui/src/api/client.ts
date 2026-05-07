import axios, { type AxiosError, type InternalAxiosRequestConfig } from "axios";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api/v1";

export const apiClient = axios.create({
  baseURL: API_BASE,
  timeout: 15000,
  headers: { "Content-Type": "application/json" },
});

apiClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = localStorage.getItem("auth_token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  const csrf = document.cookie
    .split("; ")
    .find((row) => row.startsWith("csrf_token="))
    ?.split("=")[1];
  if (csrf) {
    config.headers["X-CSRF-Token"] = csrf;
  }
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    if (error.response?.status === 401) {
      localStorage.removeItem("auth_token");
      window.location.href = "/login";
      return Promise.reject(error);
    }
    if (error.response?.status && error.response.status >= 500) {
      import("sonner").then(({ toast }) => {
        toast.error("Server Error", {
          description: "Something went wrong. Please try again later.",
        });
      });
    }
    if (!error.response && error.code === "ECONNABORTED") {
      const config = error.config as (InternalAxiosRequestConfig & { _retries?: number }) | undefined;
      if (config) {
        const retries = config._retries || 0;
        if (retries < 2) {
          config._retries = retries + 1;
          return apiClient(config);
        }
      }
    }
    return Promise.reject(error);
  }
);
