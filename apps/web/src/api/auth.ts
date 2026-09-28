// Typed calls of the authentication API (PAW-022 / PAW-023 / PAW-024; see
// apps/backend/paw_backend/api/v1/auth.py, passkeys.py and accounts.py).
import { apiRequest } from "./client";

export type SystemRole = "owner" | "admin" | "user";
export type PasskeyGate = "open" | "enrollment_required" | "assertion_required";

export interface User {
  id: string;
  login_name: string;
  system_role: SystemRole | string;
}

export interface SessionInfo {
  id: string;
  created_at: string;
  last_used_at: string;
  expires_at: string;
  absolute_expires_at: string;
  remember_me: boolean;
  device_name: string | null;
  current: boolean;
}

export interface AuthState {
  method: string;
  passkey: {
    requirement: string;
    enrolled: boolean;
    enrollment_required: boolean;
    recommended: boolean;
    available: boolean;
    gate: PasskeyGate | string;
    next: string | null;
  };
  step_up: {
    method: string | null;
    verified_at: string | null;
    valid_until: string | null;
    window_minutes: number;
    satisfied: boolean;
  };
}

export interface SessionResponse {
  user: User;
  session: SessionInfo;
  /** `null` only right after a committed change whose state read failed. */
  auth: AuthState | null;
}

export interface Passkey {
  id: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
  backup_eligible: boolean;
  backed_up: boolean;
}

export interface PairingIssued {
  pairing_id: string;
  token: string;
  link_path: string;
  expires_at: string;
  approval_required: boolean;
}

export interface PendingPairing {
  pairing_id: string;
  device_name: string | null;
  claimed_at: string;
  expires_at: string;
}

export interface PairingProgress {
  status: "completed" | "pending_approval";
  session: SessionResponse | null;
  claim: string | null;
  confirmation_code: string | null;
  expires_at: string | null;
}

type Json = Record<string, unknown>;

export const authApi = {
  session: () => apiRequest<SessionResponse>("GET", "/auth/session"),
  login: (body: {
    login_name: string;
    password: string;
    remember_me: boolean;
    device_name?: string | null;
  }) => apiRequest<SessionResponse>("POST", "/auth/login", body),
  logout: () => apiRequest<void>("POST", "/auth/logout"),
  sessions: () => apiRequest<{ sessions: SessionInfo[] }>("GET", "/auth/sessions"),
  revokeSession: (id: string) =>
    apiRequest<void>("DELETE", `/auth/sessions/${encodeURIComponent(id)}`),
  revokeOtherSessions: () =>
    apiRequest<{ revoked: number }>("POST", "/auth/sessions/revoke-others"),
  stepUpWithPassword: (password: string) =>
    apiRequest<SessionResponse>("POST", "/auth/step-up", { method: "password", password }),

  passkeyEnrollBegin: () => apiRequest<{ options: Json }>("POST", "/auth/passkeys/enroll/begin"),
  passkeyEnrollFinish: (credential: Json, name: string | null) =>
    apiRequest<{ passkey: Passkey; session: SessionResponse | null }>(
      "POST",
      "/auth/passkeys/enroll/finish",
      name ? { credential, name } : { credential },
    ),
  passkeyAuthenticateBegin: () =>
    apiRequest<{ options: Json }>("POST", "/auth/passkeys/authenticate/begin"),
  passkeyAuthenticateFinish: (credential: Json) =>
    apiRequest<SessionResponse>("POST", "/auth/passkeys/authenticate/finish", { credential }),
  passkeys: () => apiRequest<{ passkeys: Passkey[] }>("GET", "/auth/passkeys"),
  revokePasskey: (id: string) =>
    apiRequest<{ revoked: boolean; sessions_ended: number; signed_out: boolean }>(
      "DELETE",
      `/auth/passkeys/${encodeURIComponent(id)}`,
    ),

  issuePairing: () => apiRequest<PairingIssued>("POST", "/auth/pairing"),
  revokePairing: () => apiRequest<{ revoked: number }>("DELETE", "/auth/pairing"),
  pendingPairings: () => apiRequest<{ pending: PendingPairing[] }>("GET", "/auth/pairing/pending"),
  approvePairing: (id: string, confirmationCode: string) =>
    apiRequest<void>("POST", `/auth/pairing/${encodeURIComponent(id)}/approve`, {
      confirmation_code: confirmationCode,
    }),
  rejectPairing: (id: string) =>
    apiRequest<void>("POST", `/auth/pairing/${encodeURIComponent(id)}/reject`),
  claimPairing: (body: { token: string; device_name: string; remember_me: boolean }) =>
    apiRequest<PairingProgress>("POST", "/auth/pairing/claim", body),
  completePairing: (claim: string) =>
    apiRequest<PairingProgress>("POST", "/auth/pairing/complete", { claim }),
};

// Input limits of the Backend (paw_backend/auth/limits.py, passkeys/models.py).
export const LOGIN_NAME_MAX = 128;
export const DEVICE_NAME_MAX = 64;
export const PASSKEY_NAME_MAX = 64;
