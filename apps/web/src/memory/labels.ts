// How a memory's state reads on screen (UI_DESIGN.md §6.2: Confirmed / Inferred,
// Active / Stale / Superseded, Freshness). Display only: the Backend decides.
import type { MessageKey } from "../i18n";
import type { MemoryVersion } from "./types";

export type Tone = "ok" | "warning" | "info" | "muted" | "error";

/** Whether the version is past its revalidation time or marked stale. */
export function isStale(version: MemoryVersion, now: number = Date.now()): boolean {
  if (version.stale_since) return true;
  if (
    version.freshness_policy === "revalidate" &&
    version.verified_at &&
    version.revalidate_after !== null
  ) {
    return Date.parse(version.verified_at) + version.revalidate_after * 1000 <= now;
  }
  return false;
}

export function isExpired(version: MemoryVersion, now: number = Date.now()): boolean {
  return (
    version.freshness_policy === "expiring" &&
    version.expires_at !== null &&
    Date.parse(version.expires_at) <= now
  );
}

/** An active memory a person should look at: stale, or not confirmed yet. */
export function needsReview(version: MemoryVersion, now: number = Date.now()): boolean {
  if (version.status !== "active") return false;
  return (
    isStale(version, now) ||
    version.confirmation_state === "inferred" ||
    version.confirmation_state === "observed"
  );
}

/** The state chip of the list and the detail: 確定 / 推定 / 観測 / 置き換え済み / 廃止. */
export function stateChip(version: MemoryVersion): { label: MessageKey; tone: Tone } {
  if (version.status === "superseded" || version.status === "history") {
    return { label: `memory.status.${version.status}`, tone: "muted" };
  }
  if (version.status === "deprecated") return { label: "memory.status.deprecated", tone: "error" };
  switch (version.confirmation_state) {
    case "confirmed":
      return { label: "memory.confirmation.confirmed", tone: "ok" };
    case "inferred":
      return { label: "memory.confirmation.inferred", tone: "warning" };
    case "observed":
      return { label: "memory.confirmation.observed", tone: "info" };
    case "rejected":
      return { label: "memory.confirmation.rejected", tone: "error" };
  }
}

/** The freshness word after the scope (新しい / 要確認 / 要再確認 / 期限切れ / 古い). */
export function freshnessTag(
  version: MemoryVersion,
  now: number = Date.now(),
): { label: MessageKey; tone: Tone } {
  if (version.status !== "active") return { label: "memory.fresh.old", tone: "muted" };
  if (isExpired(version, now)) return { label: "memory.fresh.expired", tone: "error" };
  if (isStale(version, now)) return { label: "memory.fresh.stale", tone: "warning" };
  if (version.confirmation_state !== "confirmed") {
    return { label: "memory.fresh.review", tone: "warning" };
  }
  return { label: "memory.fresh.new", tone: "ok" };
}

/** Who wrote a version: the name when the API gives one, else the kind of actor. */
export function actorLabel(
  version: MemoryVersion,
  selfId: string,
  t: (key: MessageKey) => string,
): string {
  if (version.actor_name) return version.actor_name;
  if (version.actor_type === "user" && version.actor_user_id === selfId) {
    return t("memory.actor.you");
  }
  return t(`memory.actor.${version.actor_type}`);
}
