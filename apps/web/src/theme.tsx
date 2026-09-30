// The colour theme (PAW-060 design canvas, Tokens / UserMenu / SettingsLanguage):
// "system" follows the OS (prefers-color-scheme), "light" / "dark" are fixed. The
// choice is a per-browser convenience in localStorage (there is no account
// preference API yet; Decision 0044), so a missing or blocked storage only means
// the default ("system") is used.
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const THEME_PREFERENCES: readonly ThemePreference[] = ["system", "light", "dark"];
const STORAGE_KEY = "paw.theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

function isPreference(value: unknown): value is ThemePreference {
  return value === "system" || value === "light" || value === "dark";
}

function storedPreference(): ThemePreference | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return isPreference(value) ? value : null;
  } catch {
    return null;
  }
}

function systemTheme(): ResolvedTheme {
  // The design's default theme is dark; without matchMedia (tests, old browsers) use it.
  if (typeof window.matchMedia !== "function") return "dark";
  return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
}

interface ThemeValue {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  setPreference: (preference: ThemePreference) => void;
}

const ThemeContext = createContext<ThemeValue | null>(null);

export function ThemeProvider({
  initialPreference,
  children,
}: {
  initialPreference?: ThemePreference;
  children: ReactNode;
}) {
  const [preference, setPreferenceState] = useState<ThemePreference>(
    () => initialPreference ?? storedPreference() ?? "system",
  );
  const [system, setSystem] = useState<ResolvedTheme>(systemTheme);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(DARK_QUERY);
    const onChange = () => setSystem(query.matches ? "dark" : "light");
    query.addEventListener?.("change", onChange);
    return () => query.removeEventListener?.("change", onChange);
  }, []);

  const resolved: ResolvedTheme = preference === "system" ? system : preference;

  // `data-theme` is set only for an explicit choice; "system" leaves it to the
  // stylesheet's prefers-color-scheme rule.
  useEffect(() => {
    const root = document.documentElement;
    if (preference === "system") delete root.dataset.theme;
    else root.dataset.theme = preference;
  }, [preference]);

  const setPreference = useCallback((next: ThemePreference) => {
    setPreferenceState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Not remembered; the choice still applies to this page.
    }
  }, []);

  const value = useMemo(
    () => ({ preference, resolved, setPreference }),
    [preference, resolved, setPreference],
  );
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeValue {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme outside ThemeProvider");
  return value;
}
