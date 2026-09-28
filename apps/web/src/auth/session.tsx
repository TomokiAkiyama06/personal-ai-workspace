// The signed-in state, as the Backend reports it (GET /auth/session). The Web App
// never decides a permission itself: the role and the passkey gate only choose
// what to show, and every request is checked again by the Backend.
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { authApi, type SessionResponse } from "../api/auth";
import { isApiError, SESSION_ENDED_EVENT } from "../api/client";

export type SessionState =
  | { status: "loading" }
  | { status: "signed_out"; reason?: "expired" }
  | { status: "signed_in"; data: SessionResponse }
  | { status: "error" };

interface SessionValue {
  state: SessionState;
  /** Take a session the Backend just returned (login, step-up, rotation). */
  accept: (data: SessionResponse) => void;
  refresh: () => Promise<void>;
  /**
   * End this session on the server. Rejects (and stays signed in) when the server
   * could not be told: the HttpOnly cookie is only revoked and cleared by a
   * successful POST /auth/logout, so showing the sign-in page would be a lie.
   */
  signOut: () => Promise<void>;
  /** A request answered 401: the session ended elsewhere. */
  expired: () => void;
}

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<SessionState>({ status: "loading" });
  // Every change of the session (accept, sign-out, a refresh) takes a new
  // generation; a refresh whose answer comes after a newer change is dropped.
  const generation = useRef(0);
  const stateRef = useRef(state);
  stateRef.current = state;

  const refresh = useCallback(async () => {
    const mine = ++generation.current;
    let next: SessionState;
    try {
      next = { status: "signed_in", data: await authApi.session() };
    } catch (error) {
      next = isApiError(error, "unauthorized") ? { status: "signed_out" } : { status: "error" };
    }
    if (mine === generation.current) setState(next);
  }, []);

  const accept = useCallback(
    (data: SessionResponse) => {
      generation.current++;
      setState({ status: "signed_in", data });
      // A committed change whose state read failed: fetch the full state.
      if (data.auth === null) void refresh();
    },
    [refresh],
  );

  const signOut = useCallback(async () => {
    try {
      await authApi.logout();
    } catch (error) {
      // Already ended on the server: signed out. Anything else: still signed in.
      if (!isApiError(error, "unauthorized")) throw error;
    }
    generation.current++;
    setState({ status: "signed_out" });
  }, []);

  const expired = useCallback(() => {
    generation.current++;
    setState({ status: "signed_out", reason: "expired" });
  }, []);

  // Any request of a signed-in page that finds the session ended: back to the login.
  useEffect(() => {
    const onEnded = () => {
      // Only a signed-in page's request ends a session; before that (loading,
      // signed out) the event changes nothing and a pending refresh still counts.
      if (stateRef.current.status !== "signed_in") return;
      generation.current++;
      setState({ status: "signed_out", reason: "expired" });
    };
    window.addEventListener(SESSION_ENDED_EVENT, onEnded);
    return () => window.removeEventListener(SESSION_ENDED_EVENT, onEnded);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo(
    () => ({ state, accept, refresh, signOut, expired }),
    [state, accept, refresh, signOut, expired],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession outside SessionProvider");
  return value;
}

/** The signed-in session; only for components rendered while signed in. */
export function useSignedIn(): SessionResponse {
  const { state } = useSession();
  if (state.status !== "signed_in") throw new Error("useSignedIn while not signed in");
  return state.data;
}

/** Whether to SHOW administration entries. The Backend still enforces the role. */
export function showsAdmin(role: string): boolean {
  return role === "owner" || role === "admin";
}
