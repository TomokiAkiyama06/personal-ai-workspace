// Where the プロジェクト screen gets its data. Nothing provides a source yet: the
// Backend has no project / repository / membership routes (see model.ts), so the
// screen shows that projects are not available until one is plugged in here.
import { createContext, type ReactNode, useContext } from "react";
import type { ProjectsSource } from "./model";

const ProjectsSourceContext = createContext<ProjectsSource | null>(null);

export function ProjectsSourceProvider({
  source,
  children,
}: {
  source: ProjectsSource | null;
  children: ReactNode;
}) {
  return <ProjectsSourceContext.Provider value={source}>{children}</ProjectsSourceContext.Provider>;
}

/** The projects source, or `null` while the Backend has no routes for it. */
export function useProjectsSource(): ProjectsSource | null {
  return useContext(ProjectsSourceContext);
}
