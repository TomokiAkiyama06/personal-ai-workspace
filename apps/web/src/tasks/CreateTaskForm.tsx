// 新しいタスク (issue #274): the smallest form that creates a task and puts it in
// the queue (POST /projects/{id}/tasks). It follows the design's Working Set
// dialog (PTaskWorkingSet, リポジトリを追加: a repository and its role, the role's
// description) and the projects screen's inline form (PProjectCreate). The
// person picks one of their Active projects, writes the title and the
// instruction, picks the target repository and the budget preset. The Backend
// decides everything (project.task.run, the repository's ACL, the project's
// state); the form only leaves out what it already knows cannot work (a
// project the person only views, a repository whose ACL does not allow
// writing) and shows the Backend's refusal otherwise.
import { type FormEvent, useEffect, useId, useState } from "react";
import { useI18n } from "../i18n";
import { errorMessage } from "../i18n/errors";
import type { ProjectRepository, ProjectSummary } from "../projects/model";
import { useProjectsSource } from "../projects/source";
import {
  BUDGET_PRESETS,
  type BudgetPreset,
  MAX_INSTRUCTION_LENGTH,
  MAX_TITLE_LENGTH,
  type TaskDetail,
} from "./model";
import type { TaskSource } from "./source";

type Load<T> =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: T };

/** A project the person may create a task in, as far as the list tells. */
function mayCreateIn(project: ProjectSummary): boolean {
  return project.status === "active" && project.my_role !== null && project.my_role !== "viewer";
}

/** Whether the repository's ACL override lets a task write to it (`null`: inherit). */
function writable(repository: ProjectRepository): boolean {
  return repository.acl === null || repository.acl.includes("write");
}

export function CreateTaskForm({
  source,
  onCreated,
  onCancel,
}: {
  source: TaskSource;
  onCreated: (task: TaskDetail) => void;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const titleId = useId();
  const projects = useProjectsSource();
  const [list, setList] = useState<Load<ProjectSummary[]>>({ status: "loading" });
  const [projectId, setProjectId] = useState("");
  const [repositories, setRepositories] = useState<Load<ProjectRepository[]> | null>(null);
  const [repositoryId, setRepositoryId] = useState("");
  const [title, setTitle] = useState("");
  const [instruction, setInstruction] = useState("");
  const [preset, setPreset] = useState<BudgetPreset>("standard");
  const [busy, setBusy] = useState(false);
  // One key per form: a retry after a lost answer gets the task already created.
  const [requestKey] = useState(() => crypto.randomUUID());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!projects) return;
    let cancelled = false;
    projects
      .list()
      .then((answer) => {
        if (cancelled) return;
        const usable = answer.projects.filter(mayCreateIn);
        setList({ status: "ready", data: usable });
        const [only] = usable;
        if (usable.length === 1 && only) setProjectId(only.id);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setList({ status: "error", message: errorMessage(t, caught) });
      });
    return () => {
      cancelled = true;
    };
  }, [projects, t]);

  useEffect(() => {
    setRepositoryId("");
    if (!projects || projectId === "") {
      setRepositories(null);
      return;
    }
    let cancelled = false;
    setRepositories({ status: "loading" });
    projects
      .detail(projectId)
      .then((detail) => {
        if (cancelled) return;
        setRepositories({ status: "ready", data: detail.repositories });
        const usable = detail.repositories.filter(writable);
        const [only] = usable;
        if (usable.length === 1 && only) setRepositoryId(only.id);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setRepositories({ status: "error", message: errorMessage(t, caught) });
      });
    return () => {
      cancelled = true;
    };
  }, [projects, projectId, t]);

  const ready =
    projectId !== "" && repositoryId !== "" && title.trim() !== "" && instruction.trim() !== "";

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    setError(null);
    try {
      const task = await source.createTask({
        projectId,
        title: title.trim(),
        instruction: instruction.trim(),
        repositories: [{ repositoryId, role: "target" }],
        budgetPreset: preset,
        requestKey,
      });
      onCreated(task);
    } catch (caught) {
      setError(errorMessage(t, caught));
      setBusy(false);
    }
  };

  return (
    <section className="card task-create" aria-labelledby={titleId}>
      <form className="card-row stack" onSubmit={(event) => void submit(event)}>
        <h2 id={titleId}>{t("tasks.create.title")}</h2>
        <p className="muted small">{t("tasks.create.hint")}</p>
        {!projects && (
          <p className="form-error" role="alert">
            {t("tasks.create.noProjects")}
          </p>
        )}
        {list.status === "error" && (
          <p className="form-error" role="alert">
            {list.message}
          </p>
        )}
        <label className="field">
          <span>{t("tasks.create.project")}</span>
          <select
            required
            value={projectId}
            disabled={list.status !== "ready" || busy}
            onChange={(event) => setProjectId(event.target.value)}
          >
            <option value="">
              {list.status === "loading" ? t("app.loading") : t("tasks.create.projectPick")}
            </option>
            {list.status === "ready" &&
              list.data.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
          </select>
        </label>
        {list.status === "ready" && list.data.length === 0 && (
          <p className="muted small">{t("tasks.create.noActiveProject")}</p>
        )}
        <label className="field">
          <span>{t("tasks.create.taskTitle")}</span>
          <input
            required
            maxLength={MAX_TITLE_LENGTH}
            value={title}
            disabled={busy}
            placeholder={t("tasks.create.taskTitlePlaceholder")}
            onChange={(event) => setTitle(event.target.value)}
          />
        </label>
        <label className="field">
          <span>{t("tasks.create.instruction")}</span>
          <textarea
            required
            rows={5}
            maxLength={MAX_INSTRUCTION_LENGTH}
            value={instruction}
            disabled={busy}
            placeholder={t("tasks.create.instructionPlaceholder")}
            onChange={(event) => setInstruction(event.target.value)}
          />
        </label>
        <p className="muted small">{t("tasks.create.readOnlyNote")}</p>
        <fieldset className="task-create-group" disabled={busy}>
          <legend>{t("tasks.create.target")}</legend>
          <p className="muted small">{t("tasks.create.targetHint")}</p>
          {repositories === null && (
            <p className="muted small">{t("tasks.create.targetPickProject")}</p>
          )}
          {repositories?.status === "loading" && (
            <p className="muted small" role="status">
              {t("app.loading")}
            </p>
          )}
          {repositories?.status === "error" && (
            <p className="form-error" role="alert">
              {repositories.message}
            </p>
          )}
          {repositories?.status === "ready" && repositories.data.length === 0 && (
            <p className="muted small">{t("tasks.create.noRepository")}</p>
          )}
          {repositories?.status === "ready" && repositories.data.length > 0 && (
            <div className="task-create-options">
              {repositories.data.map((repository) => {
                const allowed = writable(repository);
                return (
                  <label key={repository.id} className="radio-option task-create-option">
                    <input
                      type="radio"
                      name={`${titleId}-target`}
                      value={repository.id}
                      checked={repositoryId === repository.id}
                      disabled={!allowed}
                      onChange={() => setRepositoryId(repository.id)}
                    />
                    <span className="task-create-option-text">
                      <span>{repository.name}</span>
                      <span className="muted small mono">
                        {allowed ? repository.default_branch : t("tasks.create.targetReadOnly")}
                      </span>
                    </span>
                  </label>
                );
              })}
            </div>
          )}
        </fieldset>
        <fieldset className="task-create-group" disabled={busy}>
          <legend>{t("tasks.create.budget")}</legend>
          <div className="task-create-options">
            {BUDGET_PRESETS.map((option) => (
              <label key={option} className="radio-option task-create-option">
                <input
                  type="radio"
                  name={`${titleId}-budget`}
                  value={option}
                  checked={preset === option}
                  onChange={() => setPreset(option)}
                />
                <span className="task-create-option-text">
                  <span>{t(`tasks.budget.${option}`)}</span>
                  <span className="muted small">{t(`tasks.create.budget.${option}`)}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="actions">
          <button type="submit" disabled={busy || !ready}>
            {busy ? t("tasks.create.submitting") : t("tasks.create.submit")}
          </button>
          <button type="button" className="secondary" disabled={busy} onClick={onCancel}>
            {t("common.cancel")}
          </button>
        </div>
      </form>
    </section>
  );
}
