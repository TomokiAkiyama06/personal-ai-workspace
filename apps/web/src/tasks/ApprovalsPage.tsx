// 承認待ち (the design's MobileApproval board; issue #185 item 6, Decision 0078):
// the tool calls the person's agents asked them to approve, and the sheet that
// approves or rejects one (`/approvals/<id>`). The Backend decides: only the
// person the agent works for may decide, once, for that one call. A strong
// approval (merging to main, changing a credential) needs a Passkey
// re-authentication and is never allowed here; it can be rejected.
import { useCallback, useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { Link, useRouter } from "../router";
import { Icon } from "../shell/icons";
import {
  APPROVALS_PATH,
  type ApprovalDecision,
  approvalPath,
  decodeSegment,
  REFRESH_MS,
  shortId,
  type ToolApproval,
} from "./model";
import { type TaskSource, useTaskSource } from "./source";
import { Unavailable } from "./TasksPage";
import "./tasks.css";

function selectedApprovalId(path: string): string | null {
  if (!path.startsWith(`${APPROVALS_PATH}/`)) return null;
  const id = path.slice(APPROVALS_PATH.length + 1);
  return id ? (decodeSegment(id) ?? id) : null;
}

export function ApprovalsPage() {
  const source = useTaskSource();
  if (!source) return <Unavailable title="approvals.title" />;
  return <ApprovalsView source={source} />;
}

type Load =
  | { status: "loading" }
  | { status: "error"; error: unknown }
  | { status: "ready"; data: readonly ToolApproval[] };

function meta(approval: ToolApproval, t: ReturnType<typeof useI18n>["t"]): string {
  return [
    t("approvals.task", { task: shortId(approval.taskId) }),
    ...approval.repositories,
    approval.agent,
  ]
    .filter(Boolean)
    .join(" · ");
}

function ApprovalsView({ source }: { source: TaskSource }) {
  const { t } = useI18n();
  const { path, navigate } = useRouter();
  const selectedId = selectedApprovalId(path);
  const [load, setLoad] = useState<Load>({ status: "loading" });
  const [done, setDone] = useState<string | null>(null);
  const reload = useCallback(() => {
    source
      .listApprovals()
      .then((data) => setLoad({ status: "ready", data }))
      .catch((error: unknown) => setLoad({ status: "error", error }));
  }, [source]);
  useEffect(reload, [reload]);
  // New approvals arrive and old ones expire while the list is shown; a failed
  // background read keeps what is shown.
  const loaded = load.status === "ready";
  useEffect(() => {
    if (!loaded) return;
    const timer = window.setInterval(() => {
      source
        .listApprovals()
        .then((data) => setLoad({ status: "ready", data }))
        .catch(() => {});
    }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [loaded, source]);

  const approvals = load.status === "ready" ? load.data : [];
  // A selected approval the bounded list does not hold (an older one) is read by
  // its id; one that is gone is "not found" (Codex review of #206).
  const listed = !loaded || !selectedId || approvals.some((item) => item.id === selectedId);
  const [single, setSingle] = useState<{ id: string; approval: ToolApproval | null } | null>(null);
  useEffect(() => {
    if (listed || !selectedId) return;
    let current = true;
    source
      .getApproval(selectedId)
      .then((approval) => {
        if (current) setSingle({ id: selectedId, approval });
      })
      .catch(() => {
        if (current) setSingle({ id: selectedId, approval: null });
      });
    return () => {
      current = false;
    };
  }, [listed, selectedId, source]);
  const selected =
    approvals.find((item) => item.id === selectedId) ??
    (single !== null && single.id === selectedId ? single.approval : null);
  const missing =
    loaded && selectedId !== null && !listed && single?.id === selectedId && !single.approval;
  const close = useCallback(() => navigate(APPROVALS_PATH), [navigate]);

  return (
    <div className="approvals-screen">
      <div className="approvals-head">
        <Link to="/agents" className="icon-button" aria-label={t("approvals.back")}>
          <Icon name="back" size={18} />
        </Link>
        <h1>{t("approvals.title")}</h1>
      </div>
      <div className="approvals-body" inert={selected !== null}>
        {done && (
          <p className="small muted" role="status">
            {done}
          </p>
        )}
        {load.status === "loading" && (
          <p className="muted small" role="status">
            {t("approvals.loading")}
          </p>
        )}
        {load.status === "error" && (
          <div className="stack">
            <p className="form-error" role="alert">
              {errorMessage(t, load.error)}
            </p>
            <button type="button" className="secondary small-button" onClick={reload}>
              {t("app.retry")}
            </button>
          </div>
        )}
        {load.status === "ready" && approvals.length === 0 && (
          <p className="muted small">{t("approvals.empty")}</p>
        )}
        <ul className="plain-list approval-cards">
          {approvals.map((approval) => (
            <li key={approval.id}>
              <Link
                to={approvalPath(approval.id)}
                className={`approval-card level-${approval.level}`}
                aria-current={approval.id === selectedId ? "page" : undefined}
              >
                <span className="approval-level mono">
                  {t(`approvals.level.${approval.level}`)}
                </span>
                <span className="approval-tool">{approval.tool}</span>
                <span className="mono muted small">
                  {approval.level === "strong_approval"
                    ? `${meta(approval, t)} · ${t("approvals.passkeyNeeded")}`
                    : meta(approval, t)}
                </span>
              </Link>
            </li>
          ))}
        </ul>
        {missing && (
          <p className="form-error" role="alert">
            {t("approvals.notFound")}
          </p>
        )}
      </div>
      {selected && (
        <ApprovalSheet
          key={selected.id}
          source={source}
          approval={selected}
          onClose={close}
          onDecided={(decision) => {
            setDone(
              t(decision === "approve" ? "approvals.approved" : "approvals.rejected", {
                tool: selected.tool,
              }),
            );
            setSingle(null);
            setLoad((current) =>
              current.status === "ready"
                ? {
                    status: "ready",
                    data: current.data.filter((item) => item.id !== selected.id),
                  }
                : current,
            );
            close();
            reload();
          }}
        />
      )}
    </div>
  );
}

function ApprovalSheet({
  source,
  approval,
  onClose,
  onDecided,
}: {
  source: TaskSource;
  approval: ToolApproval;
  onClose: () => void;
  onDecided: (decision: ApprovalDecision) => void;
}) {
  const { t, formatTime } = useI18n();
  const [pending, setPending] = useState<ApprovalDecision | null>(null);
  const [error, setError] = useState<unknown>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const strong = approval.level === "strong_approval";
  useEffect(() => {
    heading.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const decide = (decision: ApprovalDecision) => {
    setPending(decision);
    setError(null);
    source
      .decideApproval(approval.id, decision)
      .then(() => onDecided(decision))
      .catch((caught: unknown) => {
        setError(caught);
        setPending(null);
      });
  };
  const [command, ...others] = approval.summary;
  const single = others.length === 0 && command !== undefined;

  return (
    <div className="sheet-layer">
      <button
        type="button"
        className="sheet-backdrop"
        aria-label={t("approvals.close")}
        onClick={onClose}
      />
      <div role="dialog" aria-modal="true" aria-labelledby="approval-question" className="sheet">
        <span className="sheet-grip" aria-hidden="true" />
        <div className="sheet-meta">
          <span className={`approval-badge level-${approval.level} mono`}>
            {t(`approvals.level.${approval.level}`)}
          </span>
          <span className="mono muted small">
            {[t("approvals.task", { task: shortId(approval.taskId) }), approval.agent]
              .filter(Boolean)
              .join(" · ")}
          </span>
        </div>
        <h2 id="approval-question" ref={heading} tabIndex={-1}>
          {t("approvals.question")}
        </h2>
        <div className="approval-detail">
          <span className="strong">{approval.tool}</span>
          {single ? (
            <code className="approval-command">{command.value}</code>
          ) : (
            approval.summary.map((item) => (
              <div key={item.name} className="approval-fact">
                <span className="muted">{item.name}</span>
                <code>{item.value}</code>
              </div>
            ))
          )}
          {approval.repositories.length > 0 && (
            <div className="approval-fact">
              <span className="muted">{t("approvals.repository")}</span>
              <span className="mono">{approval.repositories.join(", ")}</span>
            </div>
          )}
          <div className="approval-fact">
            <span className="muted">{t("approvals.expires")}</span>
            <span className="mono">{formatTime(approval.expiresAt)}</span>
          </div>
        </div>
        {error !== null && (
          <p className="form-error" role="alert">
            {errorMessage(t, error)}
          </p>
        )}
        <div className="sheet-actions">
          {!strong && (
            <button
              type="button"
              className="wide"
              disabled={pending !== null}
              aria-busy={pending === "approve"}
              onClick={() => decide("approve")}
            >
              {t("approvals.approveOnce")}
            </button>
          )}
          <button
            type="button"
            className="danger wide"
            disabled={pending !== null}
            aria-busy={pending === "reject"}
            onClick={() => decide("reject")}
          >
            {t("approvals.reject")}
          </button>
        </div>
        <div className="sheet-note">
          <Icon name="key" size={15} />
          <p>{t("approvals.note")}</p>
        </div>
      </div>
    </div>
  );
}
