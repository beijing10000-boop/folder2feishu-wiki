import { createContext, useContext } from "react";
import type { MigrationConsole } from "./useMigrationConsole";

const ConsoleContext = createContext<MigrationConsole | null>(null);

export const ConsoleProvider = ConsoleContext.Provider;

/**
 * Step views read the console through this instead of a 40-prop interface.
 * Everything lives in one hook instance owned by <App/>, so there is still a
 * single source of truth.
 */
export function useConsole(): MigrationConsole {
  const value = useContext(ConsoleContext);
  if (!value) throw new Error("useConsole must be used inside <ConsoleProvider>");
  return value;
}
