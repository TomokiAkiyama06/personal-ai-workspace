// The Memory Detail pane (UI_DESIGN.md §6.3): 本文 / 履歴 / ソース / 詳細.
// Editing writes a new version after the one the editor started from
// (optimistic lock, Decision 0034, 2); if someone else wrote in between, the
// Backend answers `memory_version_conflict` and the editor sees the design's
// conflict notice with the difference before saving again.
import { type FormEvent, useEffect, useId, useMemo, useState } from "react";
import { isApiError } from "../api/client";
import { useSignedIn } from "../auth/session";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { Link } from "../router";
import { DiffView } from "./DiffView";
import { layoutHistory } from "./graph";
import { HistoryGraph, VersionCard } from "./HistoryGraph";
import { actorLabel, freshnessTag, isExpired, stateChip } from "./labels";
import type { MemorySource } from "./source";
import type { MemorySourceRecord, MemoryVersion } from "./types";
import { useLoad } from "./useLoad";

type Tab = "body" | "history" | "sources" | "details";
const TABS: readonly Tab[] = ["body", "history", "sources", "details"];
const EDITABLE_SCOPES = new Set(["user", "project"]);

type Field = "title" | "content";

interface Draft {
  title: string;
  content: string;
  reason: string;
}

function WarningIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <path d="M12 3.5 21 19.5H3Z" />
      <path d="M12 10v4" />
      <path d="M12 17v.4" />
    </svg>
  );
}

function ConflictNotice({
  latest,
  selfId,
  draft,
  onDiscard,
  onClose,
}: {
  latest: MemoryVersion;
  selfId: string;
  /** The editor's unsaved text; null after a restore (nothing to compare). */
  draft: Draft | null;
  onDiscard: () => void;
  onClose: () => void;
}) {
  const { t, formatTime } = useI18n();
  const [showDiff, setShowDiff] = useState(false);
  return (
    <div className="memory-conflict-wrap">
      <div className="memory-conflict" role="alert">
        <WarningIcon />
        <div className="memory-conflict-text">
          <strong>{t("memory.conflict.title")}</strong>
          <span>
            {t(draft ? "memory.conflict.body" : "memory.conflict.bodyReloaded", {
              time: formatTime(latest.created_at),
              actor: actorLabel(latest, selfId, t),
            })}
          </span>
        </div>
        <div className="memory-conflict-actions">
          {draft ? (
            <>
              <button
                type="button"
                className="secondary small-button"
                aria-expanded={showDiff}
                onClick={() => setShowDiff((value) => !value)}
              >
                {showDiff ? t("memory.conflict.hideDiff") : t("memory.conflict.diff")}
              </button>
              <button type="button" className="text-button small-button" onClick={onDiscard}>
                {t("memory.conflict.discard")}
              </button>
            </>
          ) : (
            <button type="button" className="text-button small-button" onClick={onClose}>
              {t("common.close")}
            </button>
          )}
        </div>
      </div>
      {draft && showDiff && (
        <DiffView
          before={latest.content}
          after={draft.content}
          beforeLabel={t("memory.diff.latest", { number: latest.version_number })}
          afterLabel={t("memory.diff.mine")}
        />
      )}
    </div>
  );
}

function SourcesTab({
  source,
  memoryId,
  version,
}: {
  source: MemorySource;
  memoryId: string;
  version: MemoryVersion;
}) {
  const { t, formatDate } = useI18n();
  const loaded = useLoad(
    () => source.sources(memoryId, version.version_number),
    [source, memoryId, version.version_number],
  );
  const label = (record: MemorySourceRecord): string => {
    switch (record.source_type) {
      case "conversation":
        return record.message_id
          ? t("memory.source.message", { id: record.message_id })
          : t("memory.source.conversation", { id: record.conversation_id ?? "" });
      case "task":
        return t("memory.source.task", { id: record.source_ref ?? "" });
      default:
        return t(`memory.source.${record.source_type}`);
    }
  };
  return (
    <div className="stack">
      <h3 className="section-label">
        {t("memory.sources.heading", { number: version.version_number })}
      </h3>
      {loaded.loading && !loaded.data ? (
        <p className="muted small" role="status">
          {t("app.loading")}
        </p>
      ) : loaded.error ? (
        <LoadError error={loaded.error} onRetry={loaded.reload} />
      ) : loaded.data && loaded.data.length > 0 ? (
        <ul className="memory-sources">
          {loaded.data.map((record, index) => {
            const deleted = record.source_deleted_at !== null;
            return (
              <li key={index} className={deleted ? "deleted" : undefined}>
                <span className="memory-source-name">{label(record)}</span>
                {record.source_ref &&
                  record.source_type !== "task" &&
                  record.source_type !== "conversation" && (
                    <span className="mono muted ellipsis">{record.source_ref}</span>
                  )}
                <span className="mono muted small">{formatDate(record.created_at)}</span>
                {deleted && (
                  <span className="status-chip muted-chip">
                    {t("memory.source.deleted", {
                      date: formatDate(record.source_deleted_at ?? ""),
                    })}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="muted small">{t("memory.sources.empty")}</p>
      )}
    </div>
  );
}

function duration(seconds: number, t: ReturnType<typeof useI18n>["t"]): string {
  const days = seconds / 86_400;
  if (Number.isInteger(days)) return t("memory.duration.days", { count: days });
  const hours = Math.round(seconds / 3_600);
  return t("memory.duration.hours", { count: hours });
}

function DetailsTab({ version }: { version: MemoryVersion }) {
  const { t, formatDate } = useI18n();
  const rows: [string, string][] = [
    [t("memory.facts.version"), `v${version.version_number}`],
    [t("memory.facts.scope"), t(`memory.scopeChip.${version.scope}`)],
    [t("memory.facts.type"), version.memory_type],
    [t("memory.facts.status"), t(`memory.status.${version.status}`)],
    [t("memory.facts.confirmation"), t(`memory.confirmation.${version.confirmation_state}`)],
    [t("memory.facts.importance"), String(version.importance)],
    [t("memory.facts.pinned"), version.pinned ? t("memory.yes") : t("memory.no")],
    [t("memory.facts.freshness"), t(`memory.policy.${version.freshness_policy}`)],
  ];
  if (version.verified_at) rows.push([t("memory.facts.verified"), formatDate(version.verified_at)]);
  if (version.revalidate_after !== null) {
    rows.push([t("memory.facts.revalidateAfter"), duration(version.revalidate_after, t)]);
  }
  if (version.revalidate_triggers.length > 0) {
    rows.push([t("memory.facts.triggers"), version.revalidate_triggers.join(", ")]);
  }
  if (version.expires_at) rows.push([t("memory.facts.expires"), formatDate(version.expires_at)]);
  if (version.commit_sha) rows.push([t("memory.facts.commit"), version.commit_sha.slice(0, 12)]);
  if (version.branch) rows.push([t("memory.facts.branch"), version.branch]);
  if (version.stale_since) rows.push([t("memory.facts.stale"), formatDate(version.stale_since)]);
  rows.push([t("memory.facts.created"), formatDate(version.created_at)]);
  rows.push([t("memory.facts.id"), version.memory_id]);
  return (
    <dl className="facts memory-facts">
      {rows.map(([term, value]) => (
        <div key={term}>
          <dt>{term}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function LoadError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const { t } = useI18n();
  return (
    <div className="memory-empty" role="alert">
      <p>{errorMessage(t, error)}</p>
      <button type="button" className="secondary small-button" onClick={onRetry}>
        {t("app.retry")}
      </button>
    </div>
  );
}

export function MemoryDetail({
  source,
  memoryId,
  onChanged,
}: {
  source: MemorySource;
  memoryId: string;
  /** A new version was written (the list's current versions moved on). */
  onChanged: () => void;
}) {
  const { t, formatDate } = useI18n();
  const { user } = useSignedIn();
  const tabsId = useId();
  const history = useLoad(() => source.history(memoryId), [source, memoryId]);
  const layout = useMemo(() => (history.data ? layoutHistory(history.data) : null), [history.data]);
  const current = layout?.nodes.find((node) => node.current)?.version ?? null;

  const [tab, setTab] = useState<Tab>("body");
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [base, setBase] = useState<number | null>(null);
  // The fields the editor typed in: only these are sent (when they differ from
  // the current version), so a save never writes back a stale copy of a field
  // someone else changed meanwhile.
  const [touched, setTouched] = useState<ReadonlySet<Field>>(() => new Set());
  const [conflict, setConflict] = useState<MemoryVersion | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  // A newly loaded history selects its current version unless one is chosen.
  useEffect(() => {
    if (!layout) return;
    setSelected((value) =>
      value && layout.nodes.some((node) => node.version.version_id === value)
        ? value
        : (layout.nodes.find((node) => node.current)?.version.version_id ?? null),
    );
  }, [layout]);

  if (!history.data || !layout) {
    if (history.error) return <LoadError error={history.error} onRetry={history.reload} />;
    return (
      <p className="muted small memory-detail-loading" role="status">
        {t("app.loading")}
      </p>
    );
  }
  if (!current) {
    return <p className="memory-empty muted">{t("memory.detail.none")}</p>;
  }

  const selectedNode =
    layout.nodes.find((node) => node.version.version_id === selected) ??
    layout.nodes.find((node) => node.current);
  const chip = stateChip(current);
  // The Backend's answer, when the API gives it (a project Viewer reads, not writes).
  const canWrite = history.data.can_write !== false;
  // An edit carries the freshness over, and a person cannot write session_only or
  // an expiry that has passed (Decision 0034, 4); this screen does not ask for a
  // new freshness, so those memories are not offered for editing.
  const editable =
    canWrite &&
    current.status === "active" &&
    EDITABLE_SCOPES.has(current.scope) &&
    current.freshness_policy !== "session_only" &&
    !isExpired(current) &&
    draft === null;

  const reloadAfterWrite = (written: MemoryVersion) => {
    setSelected(written.version_id);
    history.reload();
    onChanged();
  };

  const startEdit = () => {
    setDraft({ title: current.title, content: current.content, reason: "" });
    setTouched(new Set());
    setBase(current.version_number);
    setConflict(null);
    setNotice(null);
    setFailure(null);
    setTab("body");
  };

  const stopEdit = () => {
    setDraft(null);
    setTouched(new Set());
    setBase(null);
    setConflict(null);
    setFailure(null);
  };

  const onConflict = async (pending: { draft: Draft; touched: ReadonlySet<Field> } | null) => {
    // Read the version that won and edit on top of it from now on.
    const latest = await source.history(memoryId);
    const top = latest.versions.reduce<MemoryVersion | null>(
      (max, version) => (!max || version.version_number > max.version_number ? version : max),
      null,
    );
    history.reload();
    onChanged();
    if (!top) return;
    setConflict(top);
    setBase(top.version_number);
    if (pending) {
      // A field the editor did not touch follows the version that won.
      const keep = (field: Field) => pending.touched.has(field);
      setDraft({
        ...pending.draft,
        title: keep("title") ? pending.draft.title : top.title,
        content: keep("content") ? pending.draft.content : top.content,
      });
    }
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!draft || base === null) return;
    // After a conflict the edit goes on top of the version that won, which the
    // pane's own reload may not have brought in yet.
    const target = conflict && conflict.version_number === base ? conflict : current;
    const changed = (field: Field) => touched.has(field) && draft[field] !== target[field];
    const changes = {
      ...(changed("title") ? { title: draft.title } : {}),
      ...(changed("content") ? { content: draft.content } : {}),
      ...(draft.reason.trim() ? { reason: draft.reason.trim() } : {}),
    };
    // Nothing differs from the current version: there is no new version to write
    // (a reason alone is not a change).
    if (changes.title === undefined && changes.content === undefined) {
      stopEdit();
      return;
    }
    setBusy(true);
    setFailure(null);
    try {
      const written = await source.edit(memoryId, base, changes);
      stopEdit();
      setNotice(t("memory.form.saved", { number: written.version_number }));
      reloadAfterWrite(written);
    } catch (caught) {
      if (isApiError(caught, "memory_version_conflict")) {
        // If the latest version cannot be read either, say so and keep the draft.
        await onConflict({ draft, touched }).catch(() => setFailure(errorMessage(t, caught)));
      } else setFailure(errorMessage(t, caught));
    } finally {
      setBusy(false);
    }
  };

  const restore = async (version: MemoryVersion) => {
    setBusy(true);
    setFailure(null);
    setNotice(null);
    try {
      // After a conflict, the version that won is the one to build on, even
      // before the pane's reload brings it in.
      const expected =
        conflict && conflict.version_number > current.version_number
          ? conflict.version_number
          : current.version_number;
      const written = await source.restore(memoryId, expected, version.version_number);
      setNotice(
        t("memory.version.restored", {
          from: version.version_number,
          number: written.version_number,
        }),
      );
      reloadAfterWrite(written);
    } catch (caught) {
      if (isApiError(caught, "memory_version_conflict")) {
        await onConflict(null).catch(() => setFailure(errorMessage(t, caught)));
      } else setFailure(errorMessage(t, caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="memory-detail">
      <Link to="/memory" className="memory-back">
        <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M14.5 6 8.5 12l6 6" fill="none" stroke="currentColor" strokeWidth="2" />
        </svg>
        {t("memory.back")}
      </Link>
      {conflict && (
        <ConflictNotice
          latest={conflict}
          selfId={user.id}
          draft={draft}
          onDiscard={stopEdit}
          onClose={() => setConflict(null)}
        />
      )}
      <div className="memory-detail-head">
        <h2>{current.title}</h2>
        <span className={`status-chip tone-${chip.tone}`}>{t(chip.label)}</span>
        <span className="status-chip scope-chip">{t(`memory.scopeChip.${current.scope}`)}</span>
        {editable && (
          <button type="button" className="secondary small-button push-right" onClick={startEdit}>
            {t("memory.edit")}
          </button>
        )}
      </div>
      {history.error !== null && (
        // A reload after a write failed: what is shown may be out of date.
        <LoadError error={history.error} onRetry={history.reload} />
      )}
      {notice && (
        <p className="memory-notice small" role="status">
          {notice}
        </p>
      )}
      {failure && (
        <p className="form-error" role="alert">
          {failure}
        </p>
      )}
      <div className="memory-tabs" role="tablist" aria-label={t("memory.tabs")}>
        {TABS.map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            id={`${tabsId}-${name}`}
            aria-selected={tab === name}
            aria-controls={`${tabsId}-${name}-panel`}
            tabIndex={tab === name ? 0 : -1}
            onClick={() => setTab(name)}
            onKeyDown={(event) => {
              const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
              if (!step) return;
              const next = TABS[(TABS.indexOf(name) + step + TABS.length) % TABS.length] as Tab;
              setTab(next);
              document.getElementById(`${tabsId}-${next}`)?.focus();
            }}
          >
            {t(`memory.tab.${name}`)}
          </button>
        ))}
      </div>
      <div
        className="memory-tab-panel"
        role="tabpanel"
        id={`${tabsId}-${tab}-panel`}
        aria-labelledby={`${tabsId}-${tab}`}
      >
        {tab === "body" &&
          (draft ? (
            <form className="stack memory-edit" onSubmit={save}>
              <label className="field">
                <span>{t("memory.form.title")}</span>
                <input
                  value={draft.title}
                  required
                  onChange={(event) => {
                    setDraft({ ...draft, title: event.target.value });
                    setTouched((value) => new Set(value).add("title"));
                  }}
                />
              </label>
              <label className="field">
                <span>{t("memory.form.content")}</span>
                <textarea
                  value={draft.content}
                  required
                  rows={8}
                  onChange={(event) => {
                    setDraft({ ...draft, content: event.target.value });
                    setTouched((value) => new Set(value).add("content"));
                  }}
                />
              </label>
              <label className="field">
                <span>{t("memory.form.reason")}</span>
                <input
                  value={draft.reason}
                  onChange={(event) => setDraft({ ...draft, reason: event.target.value })}
                />
              </label>
              <p className="small muted">{t("memory.form.hint")}</p>
              <div className="actions">
                <button type="submit" className="small-button" disabled={busy}>
                  {busy ? t("memory.form.saving") : t("memory.form.save")}
                </button>
                <button type="button" className="text-button small-button" onClick={stopEdit}>
                  {t("common.cancel")}
                </button>
              </div>
            </form>
          ) : (
            <div className="stack">
              <p className="memory-body-text">{current.content}</p>
              <p className="mono muted small">
                {t("memory.graph.meta", {
                  number: current.version_number,
                  date: formatDate(current.created_at),
                  who: actorLabel(current, user.id, t),
                })}
                {" · "}
                {t(freshnessTag(current).label)}
              </p>
            </div>
          ))}
        {tab === "history" && selectedNode && (
          <div className="memory-history">
            <HistoryGraph
              layout={layout}
              selected={selectedNode.version.version_id}
              onSelect={setSelected}
              selfId={user.id}
            />
            <VersionCard
              key={selectedNode.version.version_id}
              node={selectedNode}
              layout={layout}
              current={current}
              selfId={user.id}
              canWrite={canWrite}
              restoring={busy}
              onRestore={(version) => void restore(version)}
            />
          </div>
        )}
        {tab === "sources" && selectedNode && (
          <SourcesTab
            source={source}
            memoryId={selectedNode.version.memory_id}
            version={selectedNode.version}
          />
        )}
        {tab === "details" && selectedNode && <DetailsTab version={selectedNode.version} />}
      </div>
    </div>
  );
}
