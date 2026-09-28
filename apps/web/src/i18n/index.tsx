import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { en } from "./en";
import { ja, type MessageKey } from "./ja";

export type { MessageKey } from "./ja";
export type Locale = "ja" | "en";
export type Params = Record<string, string | number>;

export const DEFAULT_LOCALE: Locale = "ja";
export const LOCALES: readonly { code: Locale; label: string }[] = [
  { code: "ja", label: "日本語" },
  { code: "en", label: "English" },
];
const STORAGE_KEY = "paw.locale";

const catalogs: Record<Locale, Record<MessageKey, string>> = { ja, en };

export function isLocale(value: unknown): value is Locale {
  return value === "ja" || value === "en";
}

/** The message of `key` in `locale` with `{name}` placeholders replaced. */
export function translate(locale: Locale, key: MessageKey, params?: Params): string {
  const template = catalogs[locale][key] ?? catalogs[DEFAULT_LOCALE][key] ?? key;
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in params ? String(params[name]) : match,
  );
}

/** A date and time in the locale's format (the browser's time zone). */
export function formatDateTime(locale: Locale, iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat(locale === "ja" ? "ja-JP" : "en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function storedLocale(): Locale | null {
  // Storage can be unavailable (private mode, blocked site data): never fatal.
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return isLocale(value) ? value : null;
  } catch {
    return null;
  }
}

interface I18nValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: MessageKey, params?: Params) => string;
  formatDate: (iso: string) => string;
}

const I18nContext = createContext<I18nValue | null>(null);

export function I18nProvider({
  initialLocale,
  children,
}: {
  initialLocale?: Locale;
  children: ReactNode;
}) {
  const [locale, setLocaleState] = useState<Locale>(
    () => initialLocale ?? storedLocale() ?? DEFAULT_LOCALE,
  );
  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Not remembered; the choice still applies to this page.
    }
  }, []);
  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);
  const value = useMemo<I18nValue>(
    () => ({
      locale,
      setLocale,
      t: (key, params) => translate(locale, key, params),
      formatDate: (iso) => formatDateTime(locale, iso),
    }),
    [locale, setLocale],
  );
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n outside I18nProvider");
  return value;
}
