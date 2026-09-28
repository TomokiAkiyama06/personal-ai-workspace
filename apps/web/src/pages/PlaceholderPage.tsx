import { type MessageKey, useI18n } from "../i18n";

/** A screen of the navigation whose content comes in a later issue. */
export function PlaceholderPage({ screen }: { screen: MessageKey }) {
  const { t } = useI18n();
  return (
    <div className="page">
      <h1>{t("placeholder.title", { screen: t(screen) })}</h1>
      <p className="muted">{t("placeholder.body")}</p>
    </div>
  );
}
