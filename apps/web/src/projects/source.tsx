// Where the プロジェクト screen gets its data: by default the Backend's project
// routes (`projectsApi`, api.ts; issue #184). A provider replaces it (the tests
// give a fake source); `null` shows that projects are not available.
import { createContext, type ReactNode, useContext } from "react";
import { projectsApi } from "./api";
import type { ProjectsSource } from "./model";

const ProjectsSourceContext = createContext<ProjectsSource | null>(projectsApi);

export function ProjectsSourceProvider({
  source,
  children,
}: {
  source: ProjectsSource | null;
  children: ReactNode;
}) {
  return <ProjectsSourceContext.Provider value={source}>{children}</ProjectsSourceContext.Provider>;
}

/** The projects source, or `null` when a provider says there is none. */
export function useProjectsSource(): ProjectsSource | null {
  return useContext(ProjectsSourceContext);
}
