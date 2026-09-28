// Typed message catalogs (Decision 0044). V1 shows Japanese only (the PAW-060
// design, SettingsLanguage: 表示言語は日本語に固定し、他言語の選択肢は画面に出さない);
// the English catalog keeps the app i18n-ready and is selectable only through
// `initialLocale` (tests, a later issue's language switch).
import { createContext, type ReactNode, useContext, useEffect, useMemo } from "react";
import { en } from "./en";
import { ja, type MessageKey } from "./ja";

export type { MessageKey } from "./ja";
export type Locale = "ja" | "en";
export type Params = Record<string, string | number>;

export const DEFAULT_LOCALE: Locale = "ja";

const catalogs: Record<Locale, Record<MessageKey, string>> = { ja, en };

/** The message of `key` in `locale` with `{name}` placeholders replaced. */
export function translate(locale: Locale, key: MessageKey, params?: Params): string {
  const template = catalogs[locale][key] ?? catalogs[DEFAULT_LOCALE][key] ?? key;
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in params ? String(params[name]) : match,
  );
}

function intlLocale(locale: Locale): string {
  return locale === "ja" ? "ja-JP" : "en-US";
}

/** A date and time in the locale's format (the browser's time zone). */
export function formatDateTime(locale: Locale, iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat(intlLocale(locale), {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

/** A time of day (HH:MM), for the compact lists of the design. */
export function formatTime(locale: Locale, iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat(intlLocale(locale), {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

interface I18nValue {
  locale: Locale;
  t: (key: MessageKey, params?: Params) => string;
  formatDate: (iso: string) => string;
  formatTime: (iso: string) => string;
}

const I18nContext = createContext<I18nValue | null>(null);

export function I18nProvider({
  initialLocale,
  children,
}: {
  initialLocale?: Locale;
  children: ReactNode;
}) {
  const locale = initialLocale ?? DEFAULT_LOCALE;
  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);
  const value = useMemo<I18nValue>(
    () => ({
      locale,
      t: (key, params) => translate(locale, key, params),
      formatDate: (iso) => formatDateTime(locale, iso),
      formatTime: (iso) => formatTime(locale, iso),
    }),
    [locale],
  );
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n outside I18nProvider");
  return value;
}
