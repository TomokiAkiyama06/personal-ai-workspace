// A fake ProjectsSource for the tests of the プロジェクト screen, with the data of
// the design canvas (Projects): ExampleProject with backend / ios, ios narrowed to
// read-only by a repository ACL override.
import { vi } from "vitest";
import type { ProjectDetail, ProjectSummary, ProjectsSource } from "../projects/model";

export const SUMMARIES: ProjectSummary[] = [
  {
    id: "p-example",
    name: "ExampleProject",
    status: "active",
    my_role: "manager",
    repository_names: ["backend", "ios"],
    deletion_scheduled_at: null,
  },
  {
    id: "p-tools",
    name: "InternalTools",
    status: "active",
    my_role: "contributor",
    repository_names: ["scripts"],
    deletion_scheduled_at: null,
  },
  {
    id: "p-notes",
    name: "ResearchNotes",
    status: "archived",
    my_role: "viewer",
    repository_names: ["docs"],
    deletion_scheduled_at: null,
  },
  {
    id: "p-legacy",
    name: "LegacyAPI",
    status: "pending_deletion",
    my_role: "manager",
    repository_names: ["legacy-api"],
    deletion_scheduled_at: "2026-10-30T03:00:00Z",
  },
];

export function exampleDetail(overrides: Partial<ProjectDetail> = {}): ProjectDetail {
  return {
    id: "p-example",
    name: "ExampleProject",
    description: null,
    status: "active",
    my_role: "manager",
    created_at: "2026-09-01T00:00:00Z",
    deletion_scheduled_at: null,
    repositories: [
      {
        id: "r-backend",
        name: "backend",
        default_branch: "main",
        source: "github_clone",
        acl: null,
        updated_at: "2026-09-30T05:28:00Z",
      },
      {
        id: "r-ios",
        name: "ios",
        default_branch: "main",
        source: "existing_path",
        acl: ["read"],
        updated_at: "2026-09-30T04:02:00Z",
      },
    ],
    members: [
      {
        user_id: "u-1",
        login_name: "Tomoki",
        role: "manager",
        status: "active",
        creator: true,
        invite_expires_at: null,
      },
      {
        user_id: "u-2",
        login_name: "Reviewer A",
        role: "contributor",
        status: "active",
        creator: false,
        invite_expires_at: null,
      },
      {
        user_id: "u-3",
        login_name: "Observer B",
        role: "viewer",
        status: "active",
        creator: false,
        invite_expires_at: null,
      },
      {
        user_id: "u-4",
        login_name: "Guest C",
        role: "contributor",
        status: "invited",
        creator: false,
        invite_expires_at: "2026-10-14T00:00:00Z",
      },
    ],
    ...overrides,
  };
}

/** A source answering from the fixtures; every method is a spy. */
export function fakeProjectsSource(
  options: { summaries?: ProjectSummary[]; details?: Record<string, ProjectDetail> } = {},
) {
  const summaries = options.summaries ?? SUMMARIES;
  const details = options.details ?? { "p-example": exampleDetail() };
  const source = {
    list: vi.fn(async () => summaries),
    detail: vi.fn(async (id: string) => {
      const detail = details[id];
      if (!detail) throw new Error(`no detail for ${id}`);
      return detail;
    }),
    create: vi.fn(async () => ({ id: "p-new" })),
    lifecycle: vi.fn(async () => undefined),
    registerRepository: vi.fn(async () => undefined),
    changeRole: vi.fn(async () => undefined),
  } satisfies ProjectsSource;
  return source;
}
