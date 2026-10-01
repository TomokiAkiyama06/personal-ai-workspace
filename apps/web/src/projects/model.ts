// What the プロジェクト screen shows (PAW-061; the PAW-060 design canvas, Projects).
//
// The screen is built against `ProjectsSource`; the production app gives it the
// Backend's project routes (api.ts, issue #184: paw_backend/api/v1/projects.py over
// ProjectService / RepositoryService), and without a source it shows that
// projects are not available. The fields are the routes' answers (the Backend's
// records Project, Member, Repository and its RepoAcl), so the source is a thin
// mapping. A repository whose ACL override closes it for the user is not listed.
//
// The role only chooses what to SHOW; the Backend decides every permission.

export type ProjectStatus = "active" | "archived" | "pending_deletion";
export type ProjectRole = "manager" | "contributor" | "viewer";
export const PROJECT_ROLES: readonly ProjectRole[] = ["manager", "contributor", "viewer"];

/** What a repository's ACL override can keep (paw_backend.authz.RepoPermission). */
export type RepoPermission = "read" | "write" | "agent";
export const REPO_PERMISSIONS: readonly RepoPermission[] = ["read", "write", "agent"];

/** How a repository came into the project (paw_backend.repositories.RepositorySource). */
export type RepositorySource = "github_clone" | "existing_path" | "new_local" | "new_github";
export const REPOSITORY_SOURCES: readonly RepositorySource[] = [
  "github_clone",
  "existing_path",
  "new_local",
  "new_github",
];

export interface ProjectSummary {
  id: string;
  name: string;
  status: ProjectStatus;
  /** The signed-in user's role in the project (`null` for an invitation only). */
  my_role: ProjectRole | null;
  repository_names: string[];
  /** While Pending deletion: from when it may be purged (restorable before it). */
  deletion_scheduled_at: string | null;
}

export interface ProjectRepository {
  id: string;
  name: string;
  default_branch: string;
  source: RepositorySource;
  /** `null` = inherit the project role; otherwise the override (empty = no access). */
  acl: RepoPermission[] | null;
  updated_at: string;
}

export interface ProjectMember {
  user_id: string;
  login_name: string;
  role: ProjectRole;
  /** `invited` is an invitation that was not accepted yet. */
  status: "active" | "invited";
  creator: boolean;
  invite_expires_at: string | null;
}

export interface ProjectDetail {
  id: string;
  name: string;
  description: string | null;
  status: ProjectStatus;
  my_role: ProjectRole | null;
  created_at: string;
  deletion_scheduled_at: string | null;
  repositories: ProjectRepository[];
  members: ProjectMember[];
}

export type RepositoryRegistration =
  | { source: "existing_path"; path: string; name?: string }
  | { source: "github_clone"; url: string; name?: string; branch?: string }
  | { source: "new_local"; name: string; default_branch?: string }
  | { source: "new_github"; name: string; private: boolean; default_branch?: string };

/** The lifecycle operations of ProjectService (the purge is the Backend's job). */
export type LifecycleAction = "archive" | "unarchive" | "begin_deletion" | "restore";

export interface ProjectsSource {
  list(): Promise<ProjectSummary[]>;
  detail(projectId: string): Promise<ProjectDetail>;
  create(input: { name: string; description: string | null }): Promise<{ id: string }>;
  /** `confirmName` (the project's exact name) is required by `begin_deletion`. */
  lifecycle(projectId: string, action: LifecycleAction, confirmName?: string): Promise<void>;
  registerRepository(projectId: string, registration: RepositoryRegistration): Promise<void>;
  changeRole(projectId: string, userId: string, role: ProjectRole): Promise<void>;
}

// Input limits of the Backend (paw_backend/projects/limits.py, repositories/limits.py).
export const PROJECT_NAME_MAX = 100;
export const PROJECT_DESCRIPTION_MAX = 2000;
export const REPOSITORY_NAME_MAX = 100;
export const BRANCH_MAX = 200;
export const REMOTE_URL_MAX = 1024;
export const DEFAULT_BRANCH = "main";

/** The lifecycle operations a status offers (REQUIREMENTS.md "Project lifecycle"). */
export function lifecycleActions(status: ProjectStatus): LifecycleAction[] {
  switch (status) {
    case "active":
      return ["archive", "begin_deletion"];
    case "archived":
      return ["unarchive", "begin_deletion"];
    case "pending_deletion":
      return ["restore"];
  }
}

// What each project role gives on a repository (paw_backend.authz.policy and
// REPO_PERMISSION_OF): a Viewer reads, a Contributor and a Manager also write and
// let agents work. An ACL override only narrows it, for every member alike.
const ROLE_PERMISSIONS: Record<ProjectRole, readonly RepoPermission[]> = {
  manager: REPO_PERMISSIONS,
  contributor: REPO_PERMISSIONS,
  viewer: ["read"],
};

/** What a member of `role` may do on `repository` (the role, narrowed by the ACL). */
export function effectivePermissions(
  role: ProjectRole,
  repository: Pick<ProjectRepository, "acl">,
): RepoPermission[] {
  const granted = ROLE_PERMISSIONS[role];
  const acl = repository.acl;
  return acl === null ? [...granted] : granted.filter((permission) => acl.includes(permission));
}

/**
 * A member's access to the project's repositories, for the メンバーと権限 table:
 *   all        every repository gives all the role gives
 *   read_only  a Viewer on every repository
 *   partial    some repositories are narrowed (`full`) or closed (`limited`)
 *   none       no repository can even be read
 */
export type MemberAccess =
  | { kind: "all" }
  | { kind: "read_only" }
  | { kind: "none" }
  | { kind: "partial"; full: string[]; limited: string[]; excluded: string[] };

export function memberAccess(
  role: ProjectRole,
  repositories: readonly Pick<ProjectRepository, "name" | "acl">[],
): MemberAccess {
  const granted = ROLE_PERMISSIONS[role];
  const full: string[] = [];
  const limited: string[] = [];
  const excluded: string[] = [];
  for (const repository of repositories) {
    const effective = effectivePermissions(role, repository);
    if (effective.length === granted.length) full.push(repository.name);
    else if (effective.length === 0) excluded.push(repository.name);
    else limited.push(repository.name);
  }
  if (limited.length === 0 && excluded.length === 0) {
    return role === "viewer" ? { kind: "read_only" } : { kind: "all" };
  }
  if (full.length === 0 && limited.length === 0) return { kind: "none" };
  return { kind: "partial", full, limited, excluded };
}

/** Active projects first, then Archived, then Pending deletion; by name within each. */
export function sortProjects(projects: readonly ProjectSummary[]): ProjectSummary[] {
  const order: Record<ProjectStatus, number> = { active: 0, archived: 1, pending_deletion: 2 };
  return [...projects].sort(
    (a, b) => order[a.status] - order[b.status] || a.name.localeCompare(b.name),
  );
}
