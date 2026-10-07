// Words for System Health codes (the catalog's `health.*`), shared by the
// monitoring screen, the header chip and the stored notifications. A code this
// version does not know is shown as it is (never dropped, never guessed).
import type { MessageKey, Params } from "../i18n";
import { ja } from "../i18n/ja";
import { ageParts, isComponent, SEVERITIES, STATUSES } from "./model";

type Translate = (key: MessageKey, params?: Params) => string;

function known(key: string): key is MessageKey {
  return key in ja;
}

/** "Backup" of `recovery_backup` (the Notification Center's names). */
export function componentName(t: Translate, component: string): string {
  return isComponent(component)
    ? t(`notifications.health.component.${component}` as MessageKey)
    : component;
}

export function statusText(t: Translate, status: string): string {
  return (STATUSES as readonly string[]).includes(status)
    ? t(`health.status.${status}` as MessageKey)
    : status;
}

/** 正常 / 警告 / 異常 / 重大 */
export function severityName(t: Translate, severity: string): string {
  return (SEVERITIES as readonly string[]).includes(severity)
    ? t(`health.severityName.${severity}` as MessageKey)
    : severity;
}

/** INFO / WARNING / ERROR / CRITICAL (the Notification Policy's labels). */
export function severityLabel(t: Translate, severity: string): string {
  return (SEVERITIES as readonly string[]).includes(severity)
    ? t(`notifications.severity.${severity}` as MessageKey)
    : severity.toUpperCase();
}

function kindName(t: Translate, kind: string): string {
  return kind === "codex" || kind === "claude" ? t(`health.connection.${kind}`) : kind;
}

function roleName(t: Translate, role: string): string {
  const key = `health.role.${role}`;
  return known(key) ? t(key) : role;
}

/** A reason code (`vram_pressure`, `expired:claude`, `model_failed:main`) as a sentence. */
export function reasonText(t: Translate, reason: string): string {
  const [code = reason, argument] = reason.split(":", 2);
  const key = `health.reason.${code}`;
  if (!known(key)) return reason;
  if (argument === undefined) return t(key);
  return t(key, { kind: kindName(t, argument), role: roleName(t, argument) });
}

/** "3 分前" of an age in seconds, or 記録なし. */
export function agoText(t: Translate, seconds: number | null): string {
  if (seconds === null) return t("health.ago.never");
  const { unit, value } = ageParts(seconds);
  return t(`health.ago.${unit}`, { value });
}
