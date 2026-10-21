import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiError } from "../api/client";
import type { ProjectsSource } from "../projects/model";
import { ProjectsSourceProvider } from "../projects/source";
import { mockApi, Providers, reply, session } from "../test/helpers";
import { exampleDetail, fakeProjectsSource } from "../test/projectsFixture";
import { fakeTaskSource } from "../test/tasks";
import { type TaskSource, TaskSourceProvider } from "./source";

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderTasks(source: TaskSource, projects: ProjectsSource | null) {
  mockApi({ "GET /auth/session": reply(200, session()) });
  window.history.replaceState(null, "", "/agents");
  return render(
    <Providers>
      <ProjectsSourceProvider source={projects}>
        <TaskSourceProvider source={source}>
          <App />
        </TaskSourceProvider>
      </ProjectsSourceProvider>
    </Providers>,
  );
}

async function openForm() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "新しいタスク" }));
  const form = await screen.findByRole("region", { name: "新しいタスク" });
  return { user, form };
}

describe("新しいタスク", () => {
  it("creates a task with its instruction, target and budget, and opens it", async () => {
    const { source, created } = fakeTaskSource();
    renderTasks(
      source,
      fakeProjectsSource({
        details: {
          "p-example": exampleDetail(),
          "p-tools": exampleDetail({ id: "p-tools", name: "InternalTools" }),
        },
      }),
    );
    const { user, form } = await openForm();

    // Only the Active projects the person may run tasks in.
    const project = within(form).getByRole("combobox", { name: "プロジェクト" });
    await waitFor(() =>
      expect(
        within(project)
          .getAllByRole("option")
          .map((option) => option.textContent),
      ).toEqual(["プロジェクトを選ぶ", "ExampleProject", "InternalTools"]),
    );
    await user.selectOptions(project, "p-example");

    // ios is read-only for this person (ACL override): it cannot be the target;
    // backend, the only writable one, is chosen.
    const targets = within(form).getByRole("group", { name: "Target のリポジトリ" });
    const backend = await within(targets).findByRole("radio", { name: /backend/ });
    const ios = within(targets).getByRole("radio", { name: /ios/ });
    expect(backend).toBeChecked();
    expect(ios).toBeDisabled();
    expect(within(targets).getByText("書き込み・エージェントの権限なし")).toBeInTheDocument();

    const submit = within(form).getByRole("button", { name: "作成してキューに入れる" });
    expect(submit).toBeDisabled();
    await user.type(within(form).getByRole("textbox", { name: "タイトル" }), "README を調べる");
    await user.type(
      within(form).getByRole("textbox", { name: "指示" }),
      "README の構成を読んでまとめてください。変更はしないでください。",
    );
    const budget = within(form).getByRole("group", { name: "予算" });
    expect(within(budget).getByRole("radio", { name: /標準/ })).toBeChecked();
    // Who may choose Unlimited waits for Decision 0094.
    expect(within(budget).getAllByRole("radio")).toHaveLength(2);
    await user.click(within(budget).getByRole("radio", { name: /長時間/ }));
    await user.click(submit);

    expect(created).toEqual([
      {
        projectId: "p-example",
        title: "README を調べる",
        instruction: "README の構成を読んでまとめてください。変更はしないでください。",
        repositories: [{ repositoryId: "r-backend", role: "target" }],
        budgetPreset: "long",
        requestKey: expect.stringMatching(/^[0-9a-f-]{36}$/),
        readOnly: false,
      },
    ]);
    // The form closes, the queued task is listed first and opened.
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "新しいタスク" })).not.toBeInTheDocument(),
    );
    expect(screen.getByText("タスクを作成し、キューに入れました: README を調べる")).toHaveAttribute(
      "role",
      "status",
    );
    const detail = await screen.findByRole("article", { name: "README を調べる" });
    expect(detail).toHaveTextContent("キュー待ち");
    expect(window.location.pathname).toBe("/agents/00000000-0000-4000-8000-000000000001");
    const list = screen.getByRole("navigation", { name: "タスクの一覧" });
    expect(within(list).getAllByRole("link")[0]).toHaveTextContent("README を調べる");
  });

  it("shows the Backend's refusal and keeps what was typed", async () => {
    const { source } = fakeTaskSource();
    source.createTask = vi.fn(async () => {
      throw new ApiError(403, "repository_write_forbidden", "forbidden");
    });
    const projects = fakeProjectsSource({
      summaries: [
        {
          id: "p-example",
          name: "ExampleProject",
          status: "active",
          my_role: "contributor",
          repository_names: ["backend", "ios"],
          deletion_scheduled_at: null,
        },
      ],
    });
    renderTasks(source, projects);
    const { user, form } = await openForm();

    // The only project is chosen for the person.
    const targets = within(form).getByRole("group", { name: "Target のリポジトリ" });
    expect(await within(targets).findByRole("radio", { name: /backend/ })).toBeChecked();
    await user.type(within(form).getByRole("textbox", { name: "タイトル" }), "調べもの");
    await user.type(within(form).getByRole("textbox", { name: "指示" }), "読むだけ");
    await user.click(within(form).getByRole("button", { name: "作成してキューに入れる" }));

    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "このリポジトリへの書き込み権限がないため、Target にできません。",
    );
    // Sent again from the same form: the same request key (a lost answer
    // gets the task already created, not a second one).
    await user.click(within(form).getByRole("button", { name: "作成してキューに入れる" }));
    const sent = vi.mocked(source.createTask).mock.calls.map(([task]) => task.requestKey);
    expect(sent).toHaveLength(2);
    expect(sent[0]).toBe(sent[1]);
    expect(within(form).getByRole("textbox", { name: "タイトル" })).toHaveValue("調べもの");
    expect(within(form).getByRole("button", { name: "作成してキューに入れる" })).toBeEnabled();
  });

  it("creates a read-only task that references the repository it reads", async () => {
    const { source, created } = fakeTaskSource();
    const detail = exampleDetail();
    renderTasks(
      source,
      fakeProjectsSource({
        details: {
          "p-example": {
            ...detail,
            repositories: [
              ...detail.repositories,
              // Read only for this person, but agents may work on it.
              {
                id: "r-vendor",
                name: "vendor",
                default_branch: "main",
                source: "existing_path",
                acl: ["read", "agent"],
                updated_at: "2026-09-30T04:00:00Z",
              },
            ],
          },
          "p-tools": exampleDetail({ id: "p-tools", name: "InternalTools" }),
        },
      }),
    );
    const { user, form } = await openForm();
    await user.selectOptions(
      within(form).getByRole("combobox", { name: "プロジェクト" }),
      "p-example",
    );
    const targets = within(form).getByRole("group", { name: "Target のリポジトリ" });
    expect(await within(targets).findByRole("radio", { name: /backend/ })).toBeChecked();
    expect(within(targets).getByRole("radio", { name: /vendor/ })).toBeDisabled();

    await user.click(within(form).getByRole("checkbox", { name: "読み取りだけ" }));

    // The repository is now one to read: vendor may be read, ios lets no agent in.
    const reads = within(form).getByRole("group", { name: "読むリポジトリ" });
    expect(
      within(reads).getByText("このリポジトリを読むだけで、書き込みはしません。"),
    ).toBeVisible();
    expect(within(reads).getByRole("radio", { name: /backend/ })).toBeChecked();
    const vendor = within(reads).getByRole("radio", { name: /vendor/ });
    expect(vendor).toBeEnabled();
    expect(within(reads).getByRole("radio", { name: /ios/ })).toBeDisabled();
    expect(within(reads).getByText("読み取り・エージェントの権限なし")).toBeInTheDocument();
    await user.click(vendor);
    await user.type(within(form).getByRole("textbox", { name: "タイトル" }), "vendor を調べる");
    await user.type(
      within(form).getByRole("textbox", { name: "指示" }),
      "構成をまとめてください。",
    );
    await user.click(within(form).getByRole("button", { name: "作成してキューに入れる" }));

    expect(created).toEqual([
      expect.objectContaining({
        repositories: [{ repositoryId: "r-vendor", role: "referenced" }],
        readOnly: true,
      }),
    ]);
  });

  it("unchooses a repository the task cannot use when 読み取りだけ is turned off", async () => {
    const { source } = fakeTaskSource();
    const detail = exampleDetail();
    renderTasks(
      source,
      fakeProjectsSource({
        details: {
          "p-example": {
            ...detail,
            repositories: [
              ...detail.repositories,
              {
                id: "r-vendor",
                name: "vendor",
                default_branch: "main",
                source: "existing_path",
                acl: ["read", "agent"],
                updated_at: "2026-09-30T04:00:00Z",
              },
            ],
          },
          "p-tools": exampleDetail({ id: "p-tools", name: "InternalTools" }),
        },
      }),
    );
    const { user, form } = await openForm();
    const readOnly = within(form).getByRole("checkbox", { name: "読み取りだけ" });
    await user.click(readOnly);
    await user.selectOptions(
      within(form).getByRole("combobox", { name: "プロジェクト" }),
      "p-example",
    );
    const reads = within(form).getByRole("group", { name: "読むリポジトリ" });
    await user.click(await within(reads).findByRole("radio", { name: /vendor/ }));

    await user.click(readOnly);

    // vendor cannot be written to: backend, the only target, is chosen instead.
    const targets = within(form).getByRole("group", { name: "Target のリポジトリ" });
    expect(within(targets).getByRole("radio", { name: /vendor/ })).not.toBeChecked();
    expect(within(targets).getByRole("radio", { name: /backend/ })).toBeChecked();
  });

  it("leaves out projects the person only views or that are not active", async () => {
    const { source } = fakeTaskSource();
    renderTasks(
      source,
      fakeProjectsSource({
        summaries: [
          {
            id: "p-notes",
            name: "ResearchNotes",
            status: "archived",
            my_role: "manager",
            repository_names: ["docs"],
            deletion_scheduled_at: null,
          },
          {
            id: "p-view",
            name: "Viewed",
            status: "active",
            my_role: "viewer",
            repository_names: ["docs"],
            deletion_scheduled_at: null,
          },
        ],
      }),
    );
    const { form } = await openForm();
    expect(
      await within(form).findByText(/タスクを作れるプロジェクトがありません/),
    ).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "作成してキューに入れる" })).toBeDisabled();
  });

  it("says so when projects cannot be read, and closes on Cancel", async () => {
    const { source } = fakeTaskSource();
    renderTasks(source, null);
    const { user, form } = await openForm();
    expect(within(form).getByRole("alert")).toHaveTextContent(
      "プロジェクトを読み込めないため、タスクを作れません。",
    );
    await user.click(within(form).getByRole("button", { name: "キャンセル" }));
    expect(screen.queryByRole("region", { name: "新しいタスク" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新しいタスク" })).toBeInTheDocument();
  });
});
