// プロジェクト (PAW-061; the PAW-060 design canvas, Projects): the invitation-only
// projects on the left, the selected one on the right with its tabs (概要 /
// リポジトリ / タスク / メモリ / プルリクエスト / メンバー), its repositories with
// the ACL override of each, and its members with their role and the repositories
// they can use. Creating, archiving, deleting (30 days of hold) and restoring a
// project, registering a repository and changing a member's role go through the
// same source. The role only chooses what to SHOW; the Backend decides.
import { type FormEvent, type ReactNode, useCallback, useEffect, useId, useState } from "react";
import { type MessageKey, useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import { Link, useRouter } from "../router";
import { Avatar } from "../shell/common";
import { Icon } from "../shell/icons";
import {
  BRANCH_MAX,
  DEFAULT_BRANCH,
  type LifecycleAction,
  lifecycleActions,
  memberAccess,
  PROJECT_DESCRIPTION_MAX,
  PROJECT_NAME_MAX,
  PROJECT_ROLES,
  type ProjectDetail,
  type ProjectMember,
  type ProjectRepository,
  type ProjectRole,
  type ProjectStatus,
  type ProjectSummary,
  type ProjectsSource,
  REMOTE_URL_MAX,
  REPOSITORY_NAME_MAX,
  REPOSITORY_SOURCES,
  type RepoPermission,
  type RepositoryRegistration,
  type RepositorySource,
  sortProjects,
} from "./model";
import { useProjectsSource } from "./source";
import "./projects.css";

export const PROJECTS_PATH = "/projects";

/** The project id of `/projects/<id>`, or `null` on the list itself. */
export function selectedProjectId(path: string): string | null {
  const rest = path.slice(PROJECTS_PATH.length + 1);
  if (!path.startsWith(`${PROJECTS_PATH}/`) || rest === "") return null;
  const id = rest.split("/")[0] ?? "";
  try {
    return decodeURIComponent(id) || null;
  } catch {
    return null;
  }
}

function projectPath(id: string): string {
  return `${PROJECTS_PATH}/${encodeURIComponent(id)}`;
}

type Load<T> =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: T };

function StatusChip({ status, large = false }: { status: ProjectStatus; large?: boolean }) {
  const { t } = useI18n();
  return (
    <span className={`project-status ${status}${large ? " large" : ""}`}>
      <span className="status-dot" aria-hidden="true" />
      {t(`projects.status.${status}`)}
    </span>
  );
}

function roleLabel(t: (key: MessageKey) => string, role: ProjectRole): string {
  return t(`projects.role.${role}`);
}

/** "読み取りのみ" / "読み取り・書き込み" / "アクセス不可" for an ACL override. */
function permissionsText(
  t: (key: MessageKey) => string,
  allowed: readonly RepoPermission[],
): string {
  if (allowed.length === 0) return t("projects.acl.none");
  if (allowed.length === 1 && allowed[0] === "read") return t("projects.acl.readOnly");
  return allowed.map((permission) => t(`projects.acl.${permission}`)).join("・");
}

// ---------------------------------------------------------------- list pane

function ProjectListItem({ project, current }: { project: ProjectSummary; current: boolean }) {
  const { t } = useI18n();
  const meta =
    project.repository_names.length > 0
      ? project.repository_names.join(" · ")
      : t("projects.noRepositories");
  return (
    <li>
      <Link
        to={projectPath(project.id)}
        className="project-item"
        aria-current={current ? "page" : undefined}
      >
        <span className="project-item-head">
          <span className="project-item-name ellipsis">{project.name}</span>
          <StatusChip status={project.status} />
        </span>
        <span className="mono muted ellipsis">{meta}</span>
      </Link>
    </li>
  );
}

function ProjectList({
  projects,
  selectedId,
}: {
  projects: ProjectSummary[];
  selectedId: string | null;
}) {
  const { t } = useI18n();
  const filterId = useId();
  const [filter, setFilter] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const needle = filter.trim().toLocaleLowerCase();
  const matching = sortProjects(projects).filter(
    (project) =>
      needle === "" ||
      project.name.toLocaleLowerCase().includes(needle) ||
      project.repository_names.some((name) => name.toLocaleLowerCase().includes(needle)),
  );
  // REQUIREMENTS.md: the normal list keeps Archived (and Pending deletion) apart,
  // behind 「アーカイブ済みを表示」. The selected one always stays visible.
  const active = matching.filter((project) => project.status === "active");
  const others = matching.filter((project) => project.status !== "active");
  const shownOthers =
    showArchived || needle !== "" ? others : others.filter((project) => project.id === selectedId);
  return (
    <>
      <div className="project-filter">
        <Icon name="search" size={14} />
        <label htmlFor={filterId} className="visually-hidden">
          {t("projects.filter")}
        </label>
        <input
          id={filterId}
          type="search"
          placeholder={t("projects.filterPlaceholder")}
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />
      </div>
      {projects.length === 0 ? (
        <p className="muted small">{t("projects.empty")}</p>
      ) : matching.length === 0 ? (
        <p className="muted small">{t("projects.emptyFiltered")}</p>
      ) : (
        <ul className="project-items">
          {[...active, ...shownOthers].map((project) => (
            <ProjectListItem
              key={project.id}
              project={project}
              current={project.id === selectedId}
            />
          ))}
        </ul>
      )}
      {others.length > 0 && needle === "" && (
        <button
          type="button"
          className="text-button small-button project-archived-toggle"
          aria-expanded={showArchived}
          onClick={() => setShowArchived((value) => !value)}
        >
          {showArchived
            ? t("projects.hideArchived")
            : t("projects.showArchived", { count: others.length })}
        </button>
      )}
    </>
  );
}

function ArchiveNote() {
  const { t } = useI18n();
  return <p className="project-note">{t("projects.note")}</p>;
}

// ---------------------------------------------------------------- forms

function CreateProjectForm({
  source,
  onCreated,
  onCancel,
}: {
  source: ProjectsSource;
  onCreated: (id: string) => void;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const titleId = useId();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await source.create({
        name: name.trim(),
        description: description.trim() === "" ? null : description.trim(),
      });
      onCreated(created.id);
    } catch (caught) {
      setError(errorMessage(t, caught));
      setBusy(false);
    }
  };
  return (
    <section className="card project-form" aria-labelledby={titleId}>
      <form className="card-row stack" onSubmit={(event) => void submit(event)}>
        <h2 id={titleId}>{t("projects.create.title")}</h2>
        <p className="muted small">{t("projects.create.hint")}</p>
        <label className="field">
          <span>{t("projects.create.name")}</span>
          <input
            required
            maxLength={PROJECT_NAME_MAX}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className="field">
          <span>{t("projects.create.description")}</span>
          <textarea
            rows={3}
            maxLength={PROJECT_DESCRIPTION_MAX}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="actions">
          <button type="submit" disabled={busy || name.trim() === ""}>
            {t("projects.create.submit")}
          </button>
          <button type="button" className="secondary" disabled={busy} onClick={onCancel}>
            {t("common.cancel")}
          </button>
        </div>
      </form>
    </section>
  );
}

function RegisterRepositoryForm({
  source,
  projectId,
  onRegistered,
  onCancel,
}: {
  source: ProjectsSource;
  projectId: string;
  onRegistered: () => void;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const titleId = useId();
  const [kind, setKind] = useState<RepositorySource>("github_clone");
  const [location, setLocation] = useState("");
  const [name, setName] = useState("");
  const [branch, setBranch] = useState("");
  const [isPrivate, setIsPrivate] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const needsLocation = kind === "github_clone" || kind === "existing_path";
  const optional = (value: string) => (value.trim() === "" ? undefined : value.trim());

  const registration = (): RepositoryRegistration => {
    switch (kind) {
      case "github_clone":
        return {
          source: kind,
          url: location.trim(),
          name: optional(name),
          branch: optional(branch),
        };
      case "existing_path":
        return { source: kind, path: location.trim(), name: optional(name) };
      case "new_local":
        return { source: kind, name: name.trim(), default_branch: optional(branch) };
      case "new_github":
        return {
          source: kind,
          name: name.trim(),
          private: isPrivate,
          default_branch: optional(branch),
        };
    }
  };
  const ready = needsLocation ? location.trim() !== "" : name.trim() !== "";

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await source.registerRepository(projectId, registration());
      onRegistered();
    } catch (caught) {
      setError(errorMessage(t, caught));
      setBusy(false);
    }
  };

  return (
    <form
      className="card-row stack project-form"
      aria-labelledby={titleId}
      onSubmit={(event) => void submit(event)}
    >
      <h4 id={titleId}>{t("projects.register.title")}</h4>
      <fieldset className="radio-list">
        <legend className="visually-hidden">{t("projects.register.source")}</legend>
        {REPOSITORY_SOURCES.map((option) => (
          <label key={option} className="radio-option">
            <input
              type="radio"
              name={`${titleId}-source`}
              checked={kind === option}
              onChange={() => setKind(option)}
            />
            {t(`projects.source.${option}`)}
          </label>
        ))}
      </fieldset>
      <p className="muted small">{t(`projects.register.hint.${kind}`)}</p>
      {needsLocation && (
        <label className="field">
          <span>
            {kind === "github_clone" ? t("projects.register.url") : t("projects.register.path")}
          </span>
          <input
            required
            className="mono"
            maxLength={REMOTE_URL_MAX}
            placeholder={kind === "github_clone" ? "https://github.com/owner/repo" : "/home/…"}
            value={location}
            onChange={(event) => setLocation(event.target.value)}
          />
        </label>
      )}
      <label className="field">
        <span>
          {needsLocation ? t("projects.register.nameOptional") : t("projects.register.name")}
        </span>
        <input
          required={!needsLocation}
          maxLength={REPOSITORY_NAME_MAX}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
      </label>
      {kind !== "existing_path" && (
        <label className="field">
          <span>
            {kind === "github_clone"
              ? t("projects.register.branch")
              : t("projects.register.defaultBranch")}
          </span>
          <input
            className="mono"
            maxLength={BRANCH_MAX}
            placeholder={kind === "github_clone" ? "" : DEFAULT_BRANCH}
            value={branch}
            onChange={(event) => setBranch(event.target.value)}
          />
        </label>
      )}
      {kind === "new_github" && (
        <label className="checkbox">
          <input
            type="checkbox"
            checked={isPrivate}
            onChange={(event) => setIsPrivate(event.target.checked)}
          />
          {t("projects.register.private")}
        </label>
      )}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="submit" disabled={busy || !ready}>
          {t("projects.register.submit")}
        </button>
        <button type="button" className="secondary" disabled={busy} onClick={onCancel}>
          {t("common.cancel")}
        </button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------- lifecycle

function LifecyclePanel({
  source,
  project,
  onChanged,
  onClose,
}: {
  source: ProjectsSource;
  project: Pick<ProjectDetail, "id" | "name" | "status" | "deletion_scheduled_at">;
  onChanged: () => void;
  onClose: () => void;
}) {
  const { t, formatDate } = useI18n();
  const titleId = useId();
  const [confirmName, setConfirmName] = useState("");
  const [busy, setBusy] = useState<LifecycleAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = async (action: LifecycleAction) => {
    setBusy(action);
    setError(null);
    try {
      await source.lifecycle(
        project.id,
        action,
        action === "begin_deletion" ? confirmName : undefined,
      );
      setBusy(null);
      setConfirmName("");
      onChanged();
    } catch (caught) {
      setError(errorMessage(t, caught));
      setBusy(null);
    }
  };
  const actions = lifecycleActions(project.status);
  return (
    <section className="card project-lifecycle" aria-labelledby={titleId}>
      <div className="card-head">
        <h3 id={titleId}>{t("projects.settings")}</h3>
        <button type="button" className="text-button small-button push-right" onClick={onClose}>
          {t("common.close")}
        </button>
      </div>
      {actions.includes("archive") && (
        <div className="card-row setting-row">
          <div>
            <strong>{t("projects.archive.title")}</strong>
            <p className="muted small">{t("projects.archive.body")}</p>
          </div>
          <button
            type="button"
            className="secondary small-button"
            disabled={busy !== null}
            onClick={() => void run("archive")}
          >
            {t("projects.archive.submit")}
          </button>
        </div>
      )}
      {actions.includes("unarchive") && (
        <div className="card-row setting-row">
          <div>
            <strong>{t("projects.unarchive.title")}</strong>
            <p className="muted small">{t("projects.unarchive.body")}</p>
          </div>
          <button
            type="button"
            className="secondary small-button"
            disabled={busy !== null}
            onClick={() => void run("unarchive")}
          >
            {t("projects.unarchive.submit")}
          </button>
        </div>
      )}
      {actions.includes("restore") && (
        <div className="card-row setting-row">
          <div>
            <strong>{t("projects.restore.title")}</strong>
            <p className="muted small">
              {project.deletion_scheduled_at
                ? t("projects.restore.bodyUntil", {
                    date: formatDate(project.deletion_scheduled_at),
                  })
                : t("projects.restore.body")}
            </p>
          </div>
          <button
            type="button"
            className="secondary small-button"
            disabled={busy !== null}
            onClick={() => void run("restore")}
          >
            {t("projects.restore.submit")}
          </button>
        </div>
      )}
      {actions.includes("begin_deletion") && (
        <form
          className="card-row danger-zone project-delete"
          onSubmit={(event) => {
            event.preventDefault();
            void run("begin_deletion");
          }}
        >
          <div className="danger-zone-text">
            <h4>{t("projects.delete.title")}</h4>
            <p className="muted small">{t("projects.delete.body")}</p>
            <label className="field">
              <span>{t("projects.delete.confirm", { name: project.name })}</span>
              <input
                autoComplete="off"
                value={confirmName}
                onChange={(event) => setConfirmName(event.target.value)}
              />
            </label>
          </div>
          <button
            type="submit"
            className="danger small-button"
            disabled={busy !== null || confirmName !== project.name}
          >
            {t("projects.delete.submit")}
          </button>
        </form>
      )}
      {error && (
        <div className="card-row">
          <p className="form-error" role="alert">
            {error}
          </p>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- detail

function StatCard({ label, value, sub }: { label: string; value: ReactNode; sub: string }) {
  return (
    <div className="project-stat">
      <span className="muted small">{label}</span>
      <span className="project-stat-value">{value}</span>
      <span className="mono muted ellipsis">{sub}</span>
    </div>
  );
}

function Overview({ detail }: { detail: ProjectDetail }) {
  const { t } = useI18n();
  const names = detail.repositories.map((repository) => repository.name);
  return (
    <div className="project-stats">
      <StatCard label={t("projects.stat.agents")} value="—" sub={t("projects.stat.later")} />
      <StatCard label={t("projects.stat.pulls")} value="—" sub={t("projects.stat.later")} />
      <StatCard label={t("projects.stat.memory")} value="—" sub={t("projects.stat.later")} />
      <StatCard
        label={t("projects.stat.repositories")}
        value={detail.repositories.length}
        sub={names.length > 0 ? names.join(" · ") : t("projects.noRepositories")}
      />
    </div>
  );
}

/** The lock of the design's 「Repo 個別設定」 (drawn here: a single-screen icon). */
function LockIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="5" y="10.5" width="14" height="9" rx="2" />
      <path d="M8.5 10.5V7.5a3.5 3.5 0 0 1 7 0v3" />
    </svg>
  );
}

function AccessCell({ repository }: { repository: ProjectRepository }) {
  const { t } = useI18n();
  if (repository.acl === null) return <span className="muted">{t("projects.acl.inherit")}</span>;
  return (
    <span className="project-override">
      <LockIcon />
      <span>
        {t("projects.acl.override")}
        <span className="project-override-detail">{permissionsText(t, repository.acl)}</span>
      </span>
    </span>
  );
}

function RepositoriesCard({
  source,
  detail,
  canManage,
  onChanged,
}: {
  source: ProjectsSource;
  detail: ProjectDetail;
  canManage: boolean;
  onChanged: () => void;
}) {
  const { t, formatDate } = useI18n();
  const titleId = useId();
  const [registering, setRegistering] = useState(false);
  // Registering needs `project.repo.add` (Manager) on an Active project.
  const canRegister = canManage && detail.status === "active";
  return (
    <section className="card project-card" aria-labelledby={titleId}>
      <div className="card-head">
        <h3 id={titleId}>{t("projects.repos.title")}</h3>
        {canRegister && !registering && (
          <button
            type="button"
            className="secondary small-button push-right"
            onClick={() => setRegistering(true)}
          >
            {t("projects.register.title")}
          </button>
        )}
      </div>
      {registering && (
        <RegisterRepositoryForm
          source={source}
          projectId={detail.id}
          onCancel={() => setRegistering(false)}
          onRegistered={() => {
            setRegistering(false);
            onChanged();
          }}
        />
      )}
      {detail.repositories.length === 0 ? (
        <p className="card-row muted small">{t("projects.repos.empty")}</p>
      ) : (
        <>
          <div className="table-head project-repo-row" aria-hidden="true">
            <span>{t("projects.repos.col.name")}</span>
            <span>{t("projects.repos.col.source")}</span>
            <span>{t("projects.repos.col.branch")}</span>
            <span>{t("projects.repos.col.access")}</span>
            <span>{t("projects.repos.col.updated")}</span>
          </div>
          <ul className="table-list">
            {detail.repositories.map((repository) => (
              <li key={repository.id} className="table-row project-repo-row">
                <span className="strong ellipsis" data-label={t("projects.repos.col.name")}>
                  {repository.name}
                </span>
                <span className="muted" data-label={t("projects.repos.col.source")}>
                  {t(`projects.source.${repository.source}`)}
                </span>
                <span className="mono muted ellipsis" data-label={t("projects.repos.col.branch")}>
                  {repository.default_branch}
                </span>
                <span data-label={t("projects.repos.col.access")}>
                  <AccessCell repository={repository} />
                </span>
                <span className="mono muted" data-label={t("projects.repos.col.updated")}>
                  {formatDate(repository.updated_at)}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function MemberRow({
  member,
  detail,
  canChangeRole,
  onChangeRole,
  busy,
}: {
  member: ProjectMember;
  detail: ProjectDetail;
  canChangeRole: boolean;
  onChangeRole: (role: ProjectRole) => void;
  busy: boolean;
}) {
  const { t, formatDate } = useI18n();
  const access = memberAccess(member.role, detail.repositories);
  let accessText: string;
  const notes: string[] = [];
  switch (access.kind) {
    case "all":
      accessText = t("projects.access.all");
      break;
    case "read_only":
      accessText = t("projects.access.readOnly");
      break;
    case "none":
      accessText = t("projects.access.none");
      break;
    case "partial":
      accessText =
        access.full.length > 0
          ? t("projects.access.only", { repos: access.full.join("・") })
          : t("projects.access.limited");
      if (access.limited.length > 0)
        notes.push(t("projects.access.limitedNote", { repos: access.limited.join("・") }));
      if (access.excluded.length > 0)
        notes.push(t("projects.access.excludedNote", { repos: access.excluded.join("・") }));
      break;
  }
  if (member.creator) notes.unshift(t("projects.member.creator"));
  if (member.status === "invited") {
    notes.unshift(
      member.invite_expires_at
        ? t("projects.member.invitedUntil", { date: formatDate(member.invite_expires_at) })
        : t("projects.member.invited"),
    );
  }
  return (
    <li className="table-row project-member-row">
      <span className="cell-main" data-label={t("projects.members.col.user")}>
        <Avatar name={member.login_name} />
        <span className="ellipsis">{member.login_name}</span>
      </span>
      <span data-label={t("projects.members.col.role")}>
        {canChangeRole && member.status === "active" ? (
          <select
            className="project-role-select"
            aria-label={t("projects.member.roleOf", { name: member.login_name })}
            value={member.role}
            disabled={busy}
            onChange={(event) => onChangeRole(event.target.value as ProjectRole)}
          >
            {PROJECT_ROLES.map((role) => (
              <option key={role} value={role}>
                {roleLabel(t, role)}
              </option>
            ))}
          </select>
        ) : (
          <span className="muted">{roleLabel(t, member.role)}</span>
        )}
      </span>
      <span className="muted" data-label={t("projects.members.col.access")}>
        {accessText}
      </span>
      <span className="muted small" data-label={t("projects.members.col.note")}>
        {notes.join(" · ")}
      </span>
    </li>
  );
}

function MembersCard({
  source,
  detail,
  canManage,
  onChanged,
}: {
  source: ProjectsSource;
  detail: ProjectDetail;
  canManage: boolean;
  onChanged: () => void;
}) {
  const { t } = useI18n();
  const titleId = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Changing roles needs `project.members.manage` (Manager) on an Active project.
  const canChangeRole = canManage && detail.status === "active";
  const change = async (member: ProjectMember, role: ProjectRole) => {
    setBusy(true);
    setError(null);
    try {
      await source.changeRole(detail.id, member.user_id, role);
      onChanged();
    } catch (caught) {
      setError(errorMessage(t, caught));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="card project-card" aria-labelledby={titleId}>
      <div className="card-head">
        <h3 id={titleId}>{t("projects.members.title")}</h3>
        <span className="muted small">{t("projects.members.hint")}</span>
      </div>
      {error && (
        <div className="card-row">
          <p className="form-error" role="alert">
            {error}
          </p>
        </div>
      )}
      {detail.members.length === 0 ? (
        <p className="card-row muted small">{t("projects.members.empty")}</p>
      ) : (
        <>
          <div className="table-head project-member-row" aria-hidden="true">
            <span>{t("projects.members.col.user")}</span>
            <span>{t("projects.members.col.role")}</span>
            <span>{t("projects.members.col.access")}</span>
            <span>{t("projects.members.col.note")}</span>
          </div>
          <ul className="table-list">
            {detail.members.map((member) => (
              <MemberRow
                key={member.user_id}
                member={member}
                detail={detail}
                canChangeRole={canChangeRole}
                busy={busy}
                onChangeRole={(role) => void change(member, role)}
              />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

const TABS = ["overview", "repositories", "tasks", "memory", "pulls", "members"] as const;
type Tab = (typeof TABS)[number];

function Tabs({
  tab,
  onChange,
  panelId,
}: {
  tab: Tab;
  onChange: (tab: Tab) => void;
  panelId: string;
}) {
  const { t } = useI18n();
  return (
    <div className="project-tabs" role="tablist" aria-label={t("projects.tabs")}>
      {TABS.map((entry) => (
        <button
          key={entry}
          type="button"
          role="tab"
          id={`${panelId}-${entry}`}
          aria-selected={tab === entry}
          aria-controls={panelId}
          onClick={() => onChange(entry)}
        >
          {t(`projects.tab.${entry}`)}
        </button>
      ))}
    </div>
  );
}

function DetailHeader({
  name,
  status,
  role,
  canManage,
  settingsOpen,
  onSettings,
}: {
  name: string;
  status: ProjectStatus;
  role: ProjectRole | null;
  canManage: boolean;
  settingsOpen: boolean;
  onSettings: () => void;
}) {
  const { t } = useI18n();
  return (
    <div className="project-detail-head">
      <h2 className="ellipsis">{name}</h2>
      <StatusChip status={status} large />
      {role && (
        <span className="count-chip">{t("projects.youAre", { role: roleLabel(t, role) })}</span>
      )}
      {canManage && (
        <button
          type="button"
          className="secondary small-button push-right"
          aria-expanded={settingsOpen}
          onClick={onSettings}
        >
          {t("projects.settings")}
        </button>
      )}
    </div>
  );
}

/** A project Pending deletion: nothing can be read, only restored (by a Manager). */
function PendingDeletionView({
  source,
  project,
  onChanged,
}: {
  source: ProjectsSource;
  project: ProjectSummary;
  onChanged: () => void;
}) {
  const { t, formatDate } = useI18n();
  const canManage = project.my_role === "manager";
  const [settingsOpen, setSettingsOpen] = useState(false);
  return (
    <>
      <DetailHeader
        name={project.name}
        status={project.status}
        role={project.my_role}
        canManage={canManage}
        settingsOpen={settingsOpen}
        onSettings={() => setSettingsOpen((value) => !value)}
      />
      <p className="notice">
        {project.deletion_scheduled_at
          ? t("projects.pending.bodyUntil", { date: formatDate(project.deletion_scheduled_at) })
          : t("projects.pending.body")}
      </p>
      {settingsOpen && (
        <LifecyclePanel
          source={source}
          project={project}
          onChanged={onChanged}
          onClose={() => setSettingsOpen(false)}
        />
      )}
    </>
  );
}

function ProjectDetailView({
  source,
  projectId,
  summary,
  listRead,
  onListChanged,
}: {
  source: ProjectsSource;
  projectId: string;
  summary: ProjectSummary | undefined;
  /** Whether the list was read (or failed): only then is a Pending deletion known. */
  listRead: boolean;
  onListChanged: () => void;
}) {
  const { t } = useI18n();
  const panelId = useId();
  const [load, setLoad] = useState<Load<ProjectDetail>>({ status: "loading" });
  const [reloads, setReloads] = useState(0);
  const [tab, setTab] = useState<Tab>("overview");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const pending = summary?.status === "pending_deletion";

  useEffect(() => {
    // A project Pending deletion cannot be read (access stopped): wait for the
    // list to say which status the project has before asking for it.
    if (pending || !listRead) return;
    let cancelled = false;
    source
      .detail(projectId)
      .then((data) => {
        if (!cancelled) setLoad({ status: "ready", data });
      })
      .catch((caught: unknown) => {
        if (!cancelled) setLoad({ status: "error", message: errorMessage(t, caught) });
      });
    return () => {
      cancelled = true;
    };
  }, [source, projectId, pending, listRead, reloads, t]);

  // A change reads the list again, and the project once the list has answered
  // (`listRead` goes false, then true): after a deletion was scheduled the list
  // says Pending deletion, which cannot be read, before the project is asked for.
  const changed = onListChanged;

  if (pending && summary) {
    return <PendingDeletionView source={source} project={summary} onChanged={onListChanged} />;
  }
  if (load.status === "loading") {
    return (
      <p className="muted" role="status">
        {t("app.loading")}
      </p>
    );
  }
  if (load.status === "error") {
    return (
      <div className="stack">
        <p className="form-error" role="alert">
          {load.message}
        </p>
        <div className="actions">
          <button
            type="button"
            className="secondary"
            onClick={() => setReloads((value) => value + 1)}
          >
            {t("app.retry")}
          </button>
        </div>
      </div>
    );
  }
  const detail = load.data;
  const canManage = detail.my_role === "manager";
  return (
    <>
      <DetailHeader
        name={detail.name}
        status={detail.status}
        role={detail.my_role}
        canManage={canManage}
        settingsOpen={settingsOpen}
        onSettings={() => setSettingsOpen((value) => !value)}
      />
      {detail.description && <p className="muted project-description">{detail.description}</p>}
      {settingsOpen && (
        <LifecyclePanel
          source={source}
          project={detail}
          onChanged={() => {
            setSettingsOpen(false);
            changed();
          }}
          onClose={() => setSettingsOpen(false)}
        />
      )}
      {detail.status === "archived" && <p className="notice">{t("projects.archivedNotice")}</p>}
      <Tabs tab={tab} onChange={setTab} panelId={panelId} />
      <div
        id={panelId}
        role="tabpanel"
        aria-labelledby={`${panelId}-${tab}`}
        className="project-panel"
      >
        {tab === "overview" && (
          <>
            <Overview detail={detail} />
            <RepositoriesCard
              source={source}
              detail={detail}
              canManage={canManage}
              onChanged={changed}
            />
            <MembersCard
              source={source}
              detail={detail}
              canManage={canManage}
              onChanged={changed}
            />
          </>
        )}
        {tab === "repositories" && (
          <RepositoriesCard
            source={source}
            detail={detail}
            canManage={canManage}
            onChanged={changed}
          />
        )}
        {tab === "members" && (
          <MembersCard source={source} detail={detail} canManage={canManage} onChanged={changed} />
        )}
        {(tab === "tasks" || tab === "memory" || tab === "pulls") && (
          <p className="muted">{t("projects.tabLater")}</p>
        )}
      </div>
    </>
  );
}

// ---------------------------------------------------------------- page

function PageHead({ count, onCreate }: { count: number | null; onCreate: (() => void) | null }) {
  const { t } = useI18n();
  return (
    <div className="projects-head">
      <h1>{t("projects.title")}</h1>
      {count !== null && <span className="muted small">{t("projects.count", { count })}</span>}
      {onCreate && (
        <button type="button" className="small-button push-right" onClick={onCreate}>
          <Icon name="plus" size={14} />
          {t("projects.new")}
        </button>
      )}
    </div>
  );
}

/** Without a source: the Backend has no project routes yet (see model.ts). */
function UnavailableProjects() {
  const { t } = useI18n();
  return (
    <div className="projects-page">
      <PageHead count={null} onCreate={null} />
      <div className="projects-body">
        <aside className="projects-list">
          <ArchiveNote />
        </aside>
        <section className="projects-detail">
          <div className="info-box project-unavailable" role="status">
            <Icon name="info" size={16} />
            <div className="stack-xs">
              <strong>{t("projects.unavailable.title")}</strong>
              <p>{t("projects.unavailable.body")}</p>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

function ConnectedProjects({ source }: { source: ProjectsSource }) {
  const { t } = useI18n();
  const { path, navigate } = useRouter();
  const selectedId = selectedProjectId(path);
  const [load, setLoad] = useState<Load<ProjectSummary[]>>({ status: "loading" });
  const [reloads, setReloads] = useState(0);
  // Which read of the list `load` answers: a project is read only once the latest
  // read answered (the previous list stays shown meanwhile).
  const [answered, setAnswered] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const mine = reloads;
    source
      .list()
      .then((data) => {
        if (cancelled) return;
        setLoad({ status: "ready", data });
        setAnswered(mine);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setLoad({ status: "error", message: errorMessage(t, caught) });
        setAnswered(mine);
      });
    return () => {
      cancelled = true;
    };
  }, [source, reloads, t]);

  const reloadList = useCallback(() => setReloads((value) => value + 1), []);
  const projects = load.status === "ready" ? load.data : [];
  const summary = projects.find((project) => project.id === selectedId);

  return (
    <div className="projects-page">
      <PageHead
        count={load.status === "ready" ? projects.length : null}
        onCreate={creating ? null : () => setCreating(true)}
      />
      {creating && (
        <CreateProjectForm
          source={source}
          onCancel={() => setCreating(false)}
          onCreated={(id) => {
            setCreating(false);
            reloadList();
            navigate(projectPath(id));
          }}
        />
      )}
      <div className={selectedId ? "projects-body has-selection" : "projects-body"}>
        <aside className="projects-list" aria-label={t("projects.listLabel")}>
          {load.status === "loading" && (
            <p className="muted small" role="status">
              {t("app.loading")}
            </p>
          )}
          {load.status === "error" && (
            <div className="stack">
              <p className="form-error" role="alert">
                {load.message}
              </p>
              <div className="actions">
                <button type="button" className="secondary small-button" onClick={reloadList}>
                  {t("app.retry")}
                </button>
              </div>
            </div>
          )}
          {load.status === "ready" && <ProjectList projects={projects} selectedId={selectedId} />}
          <ArchiveNote />
        </aside>
        <section className="projects-detail">
          {selectedId ? (
            <>
              <Link to={PROJECTS_PATH} className="back-link projects-back">
                <Icon name="back" size={16} />
                {t("projects.title")}
              </Link>
              <ProjectDetailView
                key={selectedId}
                source={source}
                projectId={selectedId}
                summary={summary}
                listRead={answered === reloads}
                onListChanged={reloadList}
              />
            </>
          ) : (
            <p className="muted projects-select">{t("projects.select")}</p>
          )}
        </section>
      </div>
    </div>
  );
}

export function ProjectsPage() {
  const source = useProjectsSource();
  return source ? <ConnectedProjects source={source} /> : <UnavailableProjects />;
}
