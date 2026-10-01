// The ProjectsSource of the production app: the project routes of the Backend
// (issue #184, apps/backend/paw_backend/api/v1/projects.py). The answers already
// have the shape of model.ts, so this is a thin mapping; every permission is the
// Backend's (a refusal comes back as an ApiError and is shown as such).
import { apiRequest } from "../api/client";
import type {
  LifecycleAction,
  ProjectDetail,
  ProjectListing,
  ProjectRole,
  ProjectsSource,
  RepositoryRegistration,
} from "./model";

/** The URL path of each lifecycle operation (POST /projects/{id}/<path>). */
const LIFECYCLE_PATHS: Record<LifecycleAction, string> = {
  archive: "archive",
  unarchive: "unarchive",
  begin_deletion: "begin-deletion",
  restore: "restore",
};

function projectPath(projectId: string): string {
  return `/projects/${encodeURIComponent(projectId)}`;
}

export const projectsApi: ProjectsSource = {
  async list() {
    const answer = await apiRequest<ProjectListing>("GET", "/projects");
    return { projects: answer.projects, truncated: answer.truncated };
  },

  detail(projectId) {
    return apiRequest<ProjectDetail>("GET", projectPath(projectId));
  },

  async create(input) {
    const created = await apiRequest<{ id: string }>("POST", "/projects", {
      name: input.name,
      description: input.description,
    });
    return { id: created.id };
  },

  async lifecycle(projectId, action, confirmName) {
    const path = `${projectPath(projectId)}/${LIFECYCLE_PATHS[action]}`;
    if (action === "begin_deletion") {
      await apiRequest("POST", path, { confirm_name: confirmName ?? "" });
    } else {
      await apiRequest("POST", path);
    }
  },

  async registerRepository(projectId, registration: RepositoryRegistration) {
    await apiRequest("POST", `${projectPath(projectId)}/repositories`, registration);
  },

  async changeRole(projectId, userId, role: ProjectRole) {
    await apiRequest(
      "PUT",
      `${projectPath(projectId)}/members/${encodeURIComponent(userId)}/role`,
      { role },
    );
  },
};
