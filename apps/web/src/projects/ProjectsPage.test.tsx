import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { mockApi, Providers, reply, session } from "../test/helpers";
import { exampleDetail, fakeProjectsSource, SUMMARIES } from "../test/projectsFixture";
import type { ProjectDetail, ProjectsSource } from "./model";
import { selectedProjectId } from "./ProjectsPage";
import { ProjectsSourceProvider } from "./source";

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderProjects(path: string, source: ProjectsSource | null, role = "user") {
  mockApi({ "GET /auth/session": reply(200, session({ role })) });
  window.history.replaceState(null, "", path);
  return render(
    <Providers>
      <ProjectsSourceProvider source={source}>
        <App />
      </ProjectsSourceProvider>
    </Providers>,
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("selectedProjectId", () => {
  it("reads the id of /projects/<id>", () => {
    expect(selectedProjectId("/projects")).toBeNull();
    expect(selectedProjectId("/projects/")).toBeNull();
    expect(selectedProjectId("/projects/p-1")).toBe("p-1");
    expect(selectedProjectId("/projects/a%20b/x")).toBe("a b");
    expect(selectedProjectId("/projects/%E0")).toBeNull();
    expect(selectedProjectId("/projectsx/p-1")).toBeNull();
  });
});

describe("ProjectsPage without a source", () => {
  it("says that projects are not available yet, and offers no action", async () => {
    renderProjects("/projects", null);
    expect(await screen.findByRole("heading", { name: "プロジェクト" })).toBeInTheDocument();
    expect(screen.getByText("プロジェクトはまだ表示できません")).toBeInTheDocument();
    expect(screen.getByText(/アーカイブが通常の整理方法です/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /新しいプロジェクト/ })).not.toBeInTheDocument();
  });

  it("shows the same state for a project's own path", async () => {
    renderProjects("/projects/p-example", null);
    expect(await screen.findByText("プロジェクトはまだ表示できません")).toBeInTheDocument();
  });
});

describe("ProjectsPage", () => {
  it("lists the active projects and keeps Archived / Pending deletion behind a toggle", async () => {
    renderProjects("/projects", fakeProjectsSource());
    const list = await screen.findByRole("complementary", { name: "プロジェクトの一覧" });
    expect(await within(list).findByText("ExampleProject")).toBeInTheDocument();
    expect(screen.getByText("招待制 · 4 件")).toBeInTheDocument();
    expect(within(list).getByText("backend · ios")).toBeInTheDocument();
    expect(within(list).queryByText("ResearchNotes")).not.toBeInTheDocument();
    expect(screen.getByText("プロジェクトを選んでください。")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(within(list).getByRole("button", { name: "アーカイブ済みを表示（2）" }));
    expect(within(list).getByText("ResearchNotes")).toBeInTheDocument();
    expect(within(list).getByText("Pending deletion")).toBeInTheDocument();

    await user.type(
      within(list).getByRole("searchbox", { name: "プロジェクトを絞り込む" }),
      "tools",
    );
    const links = within(list).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual([
      expect.stringContaining("InternalTools"),
    ]);
  });

  it("shows a project's repositories with their ACL override and its members' access", async () => {
    const source = fakeProjectsSource();
    renderProjects("/projects/p-example", source);
    expect(await screen.findByRole("heading", { name: "ExampleProject" })).toBeInTheDocument();
    expect(source.detail).toHaveBeenCalledWith("p-example");
    expect(screen.getByText("あなたは Manager")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /ExampleProject/ })).toHaveAttribute(
      "aria-current",
      "page",
    );

    const repos = screen.getByRole("region", { name: "リポジトリ" });
    const [backend, ios] = within(repos).getAllByRole("listitem");
    expect(backend).toHaveTextContent("backend");
    expect(backend).toHaveTextContent("GitHub から Clone");
    expect(backend).toHaveTextContent("Project 継承");
    expect(ios).toHaveTextContent("Repo 個別設定");
    expect(ios).toHaveTextContent("読み取りのみ");

    const members = screen.getByRole("region", { name: "メンバーと権限" });
    const rows = within(members).getAllByRole("listitem");
    // The override narrows every member alike (paw_backend.authz.policy).
    expect(rows[0]).toHaveTextContent("backend のみ");
    expect(rows[0]).toHaveTextContent("作成者");
    expect(rows[0]).toHaveTextContent("ios は Repo 設定で制限");
    // A Viewer only reads; a read-only override takes nothing more from it.
    expect(rows[2]).toHaveTextContent("読み取りのみ");
    expect(rows[3]).toHaveTextContent("招待中");
    // An invitation grants nothing until it is accepted.
    expect(rows[3]).toHaveTextContent("承諾まではなし");
    expect(rows[3]).not.toHaveTextContent("backend のみ");
    expect(within(rows[3] as HTMLElement).queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("shows 'all repositories' when no repository has an override", async () => {
    const detail = exampleDetail();
    const inherit: ProjectDetail = {
      ...detail,
      repositories: detail.repositories.map((repository) => ({ ...repository, acl: null })),
    };
    renderProjects(
      "/projects/p-example",
      fakeProjectsSource({ details: { "p-example": inherit } }),
    );
    const members = await screen.findByRole("region", { name: "メンバーと権限" });
    expect(within(members).getAllByRole("listitem")[1]).toHaveTextContent("すべての Repo");
  });

  it("changes a member's role for a Manager and reads the project again", async () => {
    const source = fakeProjectsSource();
    renderProjects("/projects/p-example", source);
    const select = await screen.findByRole("combobox", { name: "Reviewer A の役割" });
    const user = userEvent.setup();
    await user.selectOptions(select, "viewer");
    expect(source.changeRole).toHaveBeenCalledWith("p-example", "u-2", "viewer");
    await waitFor(() => expect(source.detail).toHaveBeenCalledTimes(2));
  });

  it("shows the Backend's refusal of a role change", async () => {
    const source = fakeProjectsSource();
    source.changeRole.mockRejectedValueOnce(new Error("boom"));
    renderProjects("/projects/p-example", source);
    const user = userEvent.setup();
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Tomoki の役割" }),
      "contributor",
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("client");
  });

  it("offers no management to a Contributor", async () => {
    const detail = exampleDetail({ my_role: "contributor" });
    renderProjects("/projects/p-example", fakeProjectsSource({ details: { "p-example": detail } }));
    expect(await screen.findByText("あなたは Contributor")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "プロジェクト設定" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "リポジトリを登録" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("offers the lifecycle to an Owner / Admin who is not the project's Manager", async () => {
    const source = fakeProjectsSource({
      details: { "p-example": exampleDetail({ my_role: "viewer" }) },
    });
    renderProjects("/projects/p-example", source, "admin");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "プロジェクト設定" }));
    await user.click(screen.getByRole("button", { name: "アーカイブする" }));
    expect(source.lifecycle).toHaveBeenCalledWith("p-example", "archive", undefined);
    // Repositories and members stay the Manager's.
    expect(screen.queryByRole("button", { name: "リポジトリを登録" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("offers an Owner the restore of a project Pending deletion without being its Manager", async () => {
    const summaries = SUMMARIES.map((project) =>
      project.id === "p-legacy" ? { ...project, my_role: "contributor" as const } : project,
    );
    const source = fakeProjectsSource({ summaries });
    renderProjects("/projects/p-legacy", source, "owner");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "プロジェクト設定" }));
    await user.click(screen.getByRole("button", { name: "復元する" }));
    expect(source.lifecycle).toHaveBeenCalledWith("p-legacy", "restore", undefined);
  });

  it("switches between the tabs", async () => {
    renderProjects("/projects/p-example", fakeProjectsSource());
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "メンバー" }));
    expect(screen.getByRole("tab", { name: "メンバー" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("region", { name: "メンバーと権限" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "リポジトリ" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "タスク" }));
    expect(screen.getByRole("tabpanel")).toHaveTextContent("後続の Issue");
  });

  it("creates a project and opens it", async () => {
    const source = fakeProjectsSource({
      details: {
        "p-example": exampleDetail(),
        "p-new": exampleDetail({ id: "p-new", name: "NewOne" }),
      },
    });
    renderProjects("/projects", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "新しいプロジェクト" }));
    const form = screen.getByRole("region", { name: "新しいプロジェクト" });
    const create = within(form).getByRole("button", { name: "作成" });
    expect(create).toBeDisabled();
    await user.type(within(form).getByRole("textbox", { name: "名前" }), "  NewOne ");
    await user.click(create);
    expect(source.create).toHaveBeenCalledWith({ name: "NewOne", description: null });
    expect(await screen.findByRole("heading", { name: "NewOne" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/projects/p-new");
    expect(source.list).toHaveBeenCalledTimes(2);
  });

  it("archives a project from プロジェクト設定", async () => {
    const source = fakeProjectsSource();
    renderProjects("/projects/p-example", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "プロジェクト設定" }));
    await user.click(screen.getByRole("button", { name: "アーカイブする" }));
    expect(source.lifecycle).toHaveBeenCalledWith("p-example", "archive", undefined);
    await waitFor(() => expect(source.detail).toHaveBeenCalledTimes(2));
    expect(source.list).toHaveBeenCalledTimes(2);
  });

  it("schedules a deletion only after the exact project name is typed", async () => {
    const source = fakeProjectsSource();
    renderProjects("/projects/p-example", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "プロジェクト設定" }));
    const submit = screen.getByRole("button", { name: "削除を予約する" });
    const confirm = screen.getByRole("textbox", { name: /ExampleProject/ });
    await user.type(confirm, "exampleproject");
    expect(submit).toBeDisabled();
    await user.clear(confirm);
    await user.type(confirm, "ExampleProject");
    await user.click(submit);
    expect(source.lifecycle).toHaveBeenCalledWith("p-example", "begin_deletion", "ExampleProject");
  });

  it("shows the hold after scheduling a deletion without reading the project again", async () => {
    const source = fakeProjectsSource();
    const pending = SUMMARIES.map((project) =>
      project.id === "p-example"
        ? {
            ...project,
            status: "pending_deletion" as const,
            deletion_scheduled_at: "2026-10-30T03:00:00Z",
          }
        : project,
    );
    source.lifecycle.mockImplementationOnce(async () => {
      source.list.mockResolvedValue(pending);
    });
    renderProjects("/projects/p-example", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "プロジェクト設定" }));
    await user.type(screen.getByRole("textbox", { name: /ExampleProject/ }), "ExampleProject");
    await user.click(screen.getByRole("button", { name: "削除を予約する" }));
    expect(await screen.findByText(/削除の保留中です/)).toBeInTheDocument();
    expect(source.detail).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("offers Active again for an archived project", async () => {
    const source = fakeProjectsSource({
      details: { "p-example": exampleDetail({ status: "archived" }) },
    });
    renderProjects("/projects/p-example", source);
    expect(await screen.findByText(/アーカイブ済みのため読み取り専用です/)).toBeInTheDocument();
    // Archived is read-only: no repository registration, no role change.
    expect(screen.queryByRole("button", { name: "リポジトリを登録" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "プロジェクト設定" }));
    expect(screen.queryByRole("button", { name: "アーカイブする" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Active に戻す" }));
    expect(source.lifecycle).toHaveBeenCalledWith("p-example", "unarchive", undefined);
  });

  it("does not open a project Pending deletion and offers its restore", async () => {
    const source = fakeProjectsSource();
    renderProjects("/projects/p-legacy", source);
    expect(await screen.findByRole("heading", { name: "LegacyAPI" })).toBeInTheDocument();
    expect(screen.getByText(/削除の保留中です/)).toBeInTheDocument();
    expect(source.detail).not.toHaveBeenCalledWith("p-legacy");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "プロジェクト設定" }));
    await user.click(screen.getByRole("button", { name: "復元する" }));
    expect(source.lifecycle).toHaveBeenCalledWith("p-legacy", "restore", undefined);
  });

  it("registers a repository cloned from GitHub", async () => {
    const source = fakeProjectsSource();
    renderProjects("/projects/p-example", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "リポジトリを登録" }));
    const form = screen.getByRole("form", { name: "リポジトリを登録" });
    await user.type(
      within(form).getByRole("textbox", { name: "GitHub の URL" }),
      "https://github.com/owner/web",
    );
    await user.click(within(form).getByRole("button", { name: "登録" }));
    expect(source.registerRepository).toHaveBeenCalledWith("p-example", {
      source: "github_clone",
      url: "https://github.com/owner/web",
      name: undefined,
      branch: undefined,
    });
    await waitFor(() => expect(source.detail).toHaveBeenCalledTimes(2));
  });

  it("registers a new GitHub repository with its name and privacy", async () => {
    const source = fakeProjectsSource();
    renderProjects("/projects/p-example", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "リポジトリを登録" }));
    const form = screen.getByRole("form", { name: "リポジトリを登録" });
    await user.click(within(form).getByRole("radio", { name: "新規（GitHub）" }));
    expect(within(form).queryByRole("textbox", { name: "GitHub の URL" })).not.toBeInTheDocument();
    await user.type(within(form).getByRole("textbox", { name: "名前" }), "web");
    await user.click(within(form).getByRole("checkbox", { name: "非公開のリポジトリにする" }));
    await user.click(within(form).getByRole("button", { name: "登録" }));
    expect(source.registerRepository).toHaveBeenCalledWith("p-example", {
      source: "new_github",
      name: "web",
      private: false,
      default_branch: undefined,
    });
  });

  it("drops the answer for a project that is no longer selected", async () => {
    const slow = deferred<ProjectDetail>();
    const source = fakeProjectsSource({
      details: {
        "p-example": exampleDetail(),
        "p-tools": exampleDetail({ id: "p-tools", name: "InternalTools" }),
      },
    });
    source.detail.mockImplementationOnce(() => slow.promise);
    renderProjects("/projects/p-example", source);
    const list = await screen.findByRole("complementary", { name: "プロジェクトの一覧" });
    const user = userEvent.setup();
    await user.click(await within(list).findByRole("link", { name: /InternalTools/ }));
    expect(await screen.findByRole("heading", { name: "InternalTools" })).toBeInTheDocument();
    slow.resolve(exampleDetail({ name: "Stale" }));
    await new Promise((done) => setTimeout(done, 0));
    expect(screen.queryByRole("heading", { name: "Stale" })).not.toBeInTheDocument();
  });

  it("reads a project again after a failed read", async () => {
    const source = fakeProjectsSource();
    source.detail.mockRejectedValueOnce(new Error("down"));
    renderProjects("/projects/p-example", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "再試行" }));
    expect(await screen.findByRole("heading", { name: "ExampleProject" })).toBeInTheDocument();
    expect(source.detail).toHaveBeenCalledTimes(2);
  });

  it("shows a failed read with a retry", async () => {
    const source = fakeProjectsSource();
    source.list.mockRejectedValueOnce(new Error("down"));
    renderProjects("/projects", source);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "再試行" }));
    expect(await screen.findByText("ExampleProject")).toBeInTheDocument();
    expect(SUMMARIES).toHaveLength(4);
  });
});
