// useAuth — the client-side authentication session.
//
// Owns the JWT session for the whole app so the sidebar's user chip (sign in /
// log out, bottom-left) has one source of truth, and exposes it to any other
// consumer that needs to know who is signed in.
//
// Backend contract (backend/app/routes/auth.py — repository-backed):
//   POST /api/auth/login    OAuth2 password flow -> access token + user
//   GET  /api/auth/me       re-resolves the token against the users table
//   POST /api/auth/register self-service signup (gated by SIGNUP_ENABLED)
//   POST /api/auth/logout   audit-log only; the JWT itself is stateless
//   GET  /api/auth/config   unauthenticated capabilities probe
//
// Persistence: the token is kept in localStorage so a reload does not sign the
// user out. That is deliberate for this internal banking workspace talking to a
// same-origin (Vite-proxied) API; HTTP-only cookies are the hardening step for
// a public deployment.
import { useCallback, useEffect, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { api, detailFromJsonError, setAuthToken } from '../api/client';
import type { AuthConfigPayload, AuthUserPayload } from '../api/types';
import { notify } from '../components/Toast';

export const AUTH_TOKEN_STORAGE_KEY = 'agenticdia.auth.token';

/**
 * The browser's localStorage, or null when it is unavailable (SSR / Node test
 * runs, private mode, storage disabled by policy). Every accessor below treats
 * null as "no persistence" instead of throwing.
 */
function tokenStorage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null;
  } catch {
    return null;
  }
}

/** Read the persisted token, tolerating a blocked/unavailable localStorage. */
export function readStoredToken(): string | null {
  return tokenStorage()?.getItem(AUTH_TOKEN_STORAGE_KEY) ?? null;
}

export function writeStoredToken(token: string | null): void {
  const storage = tokenStorage();
  if (!storage) return;
  if (token) storage.setItem(AUTH_TOKEN_STORAGE_KEY, token);
  else storage.removeItem(AUTH_TOKEN_STORAGE_KEY);
}

/** Initials for the sidebar avatar ('Ada Lovelace' -> 'AL'). */
export function authInitials(user: Pick<AuthUserPayload, 'full_name' | 'email'>): string {
  const source = (user.full_name || user.email || '').trim();
  const parts = source.split(/\s+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return `${parts[0][0]}${parts[parts.length - 1][0]}`.toUpperCase();
}

export interface UseAuthResult {
  /** The signed-in user's public profile, or null when signed out. */
  user: AuthUserPayload | null;
  token: string | null;
  /** True while a login / register / logout request is in flight. */
  isAuthenticating: boolean;
  /** False when the backend reports SIGNUP_ENABLED=false (hides the sign-up UI). */
  signupEnabled: boolean;
  /** Sign in with email + password. Resolves to a user-facing error, or null on success. */
  login: (email: string, password: string) => Promise<string | null>;
  /** Create an account and sign in. Resolves to a user-facing error, or null on success. */
  register: (
    email: string,
    password: string,
    fullName: string,
    role?: string,
  ) => Promise<string | null>;
  /** Clear the session (the server audit call is best-effort). */
  logout: () => Promise<void>;
  /** Permanently delete the signed-in account after re-authentication. */
  deleteAccount: (password: string, confirmation: string) => Promise<string | null>;
  /** Controls the sign-in / sign-up dialog rendered by Dashboard. */
  isAuthModalOpen: boolean;
  setIsAuthModalOpen: Dispatch<SetStateAction<boolean>>;
}

// AUTH 1.3 — Frontend session owner (layer: Frontend hook → API client).
//            One source of truth for the JWT session; every entry below is a
//            separate AUTH sub-flow:
//              login()      → AUTH 1.3.1 → api.authLogin    → POST /api/auth/login    (AUTH 1.4 → 1.7.4)
//              register()   → AUTH 6.1   → api.authRegister → POST /api/auth/register (AUTH 6.2 → 6.3)
//              logout()     → AUTH 4.1   → api.authLogout   → POST /api/auth/logout   (AUTH 4.2 → 4.4)
//              mount        → AUTH 3.1   → api.authConfig   → GET  /api/auth/config   (AUTH 3.2 → 3.3)
//              mount/restore→ AUTH 2.1   → api.authMe       → GET  /api/auth/me       (AUTH 2.2 → 2.3)
export function useAuth(): UseAuthResult {
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<AuthUserPayload | null>(null);
  const [isAuthenticating, setIsAuthenticating] = useState<boolean>(false);
  const [signupEnabled, setSignupEnabled] = useState<boolean>(false);
  const [isAuthModalOpen, setIsAuthModalOpen] = useState<boolean>(false);

  // AUTH 3.1 — Capabilities probe on mount (layer: Frontend → API client).
  //            Unauthenticated by design: the client must know whether signup is
  //            available BEFORE it holds a token. Result gates the sign-up tab
  //            offered by AUTH 1.1.1.
  // AUTH 3.4 — Fail CLOSED: if the probe fails, signupEnabled stays false.
  // ---- Public auth capabilities (unauthenticated) --------------------------
  useEffect(() => {
    let cancelled = false;
    api
      .authConfig()
      .then((config: AuthConfigPayload) => {
        if (!cancelled) setSignupEnabled(Boolean(config.signup_enabled));
      })
      .catch((err: unknown) => {
        // Fail CLOSED: if we cannot confirm signup is enabled, do not offer it.
        console.warn('GET /api/auth/config failed; sign-up stays hidden', err);
        if (!cancelled) setSignupEnabled(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // AUTH 2.1 — Session restore on mount (layer: Frontend → API client).
  //            Persisted JWT (AUTH 1.5 sink #2) → axios default header
  //            (AUTH 1.5.1) → GET /api/auth/me (AUTH 2.2) which re-resolves the
  //            token against the users table on the server (AUTH 2.3 → AUTH 7.1).
  // AUTH 2.4 — Invalid/expired/stale token: clear the local session instead of
  //            leaving the UI in a fake signed-in state.
  // ---- Restore a persisted session ----------------------------------------
  // The stored token is NOT trusted on its own: GET /api/auth/me re-resolves it
  // against the users table, so a token for a deleted account (or one signed
  // with a different secret after a backend restart) is discarded instead of
  // leaving the UI in a fake signed-in state.
  useEffect(() => {
    const stored = readStoredToken();
    if (!stored) return;

    setAuthToken(stored);
    setToken(stored);

    let cancelled = false;
    api
      .authMe(stored)
      .then((profile) => {
        if (!cancelled) setUser(profile);
      })
      .catch(() => {
        if (cancelled) return;
        console.warn('Stored session is no longer valid; signing out locally');
        setAuthToken(null);
        writeStoredToken(null);
        setToken(null);
        setUser(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // AUTH 4.3 — The actual sign-out (layer: Frontend, local-only): clears all
  //            three session sinks together — axios default header, localStorage
  //            and React state. The server call in AUTH 4.2 is audit-only because
  //            the JWT is stateless (it cannot be revoked).
  const clearSession = useCallback(() => {
    setAuthToken(null);
    writeStoredToken(null);
    setToken(null);
    setUser(null);
  }, []);

  // AUTH 1.3.1 — login(): the frontend LOGIN handler.
  //              Layer chain: Frontend → API client (AUTH 1.4) → FastAPI route
  //              (AUTH 1.7) → service (AUTH 1.7.1) → repository (AUTH 1.7.1.1).
  //              Errors: the backend returns an identical 401 "Invalid
  //              credentials" for unknown email AND wrong password (AUTH 1.7.1),
  //              so that detail is surfaced verbatim.
  const login = useCallback(async (email: string, password: string) => {
    setIsAuthenticating(true);
    try {
      const session = await api.authLogin(email.trim(), password);
      // AUTH 1.5 — Session persistence (response → frontend state). Three sinks,
      //            deliberately in this order:
      //              1) axios default Authorization header (AUTH 1.5.1)
      //              2) localStorage — survives a reload → AUTH 2.1
      //              3) React state — flips the sidebar chip → AUTH 1.6
      setAuthToken(session.access_token);
      writeStoredToken(session.access_token);
      setToken(session.access_token);
      setUser(session.user);
      notify(`Signed in as ${session.user.full_name}`, 'success');
      return null;
    } catch (err: unknown) {
      // The backend answers an identical "Invalid credentials" for both an
      // unknown email and a wrong password (by design), so surface it as-is.
      return (
        detailFromJsonError(err) ??
        'Login failed. Check your email and password, and confirm that the backend is running.'
      );
    } finally {
      setIsAuthenticating(false);
    }
  }, []);

  // AUTH 6.1 — register(): Frontend → api.authRegister (AUTH 6.2) → POST
  //            /api/auth/register (AUTH 6.3). Discrepancy vs. the expected flow:
  //            the register response carries NO token, so AUTH 6.4 signs the new
  //            account in with the same credentials via AUTH 1.3.1.
  const register = useCallback(
    async (email: string, password: string, fullName: string, role?: string) => {
      setIsAuthenticating(true);
      try {
        await api.authRegister({
          email: email.trim(),
          password,
          full_name: fullName.trim(),
          ...(role ? { role } : {}),
        });
      } catch (err: unknown) {
        return detailFromJsonError(err) ?? 'Registration failed. Please try again.';
      } finally {
        setIsAuthenticating(false);
      }

      // AUTH 6.4 — Register returns no token (AUTH 6.3.6), so chain straight into
      //            the normal login flow (AUTH 1.3.1 → AUTH 1.4 → AUTH 1.7).
      // Registration returns no token, so sign the new account in with the very
      // credentials just created (one less round-trip for the user).
      const signInError = await login(email, password);
      if (signInError) {
        notify('Account created. Please sign in.', 'success');
        return signInError;
      }
      return null;
    },
    [login],
  );

  // AUTH 4.1 — logout(): sign-out handler. Layer chain: Frontend (AuthPanel
  //            AUTH 4.1) → API client AUTH 4.2 → route AUTH 4.4 (audit log only)
  //            → local clear AUTH 4.3 → AUTH 1.6 reverts to the signed-out UI.
  const logout = useCallback(async () => {
    setIsAuthenticating(true);
    try {
      // AUTH 4.2 — Best-effort server-side call. The JWT is stateless (AUTH 1.7.3),
      //            so a failure here is not fatal — AUTH 4.3 is what actually signs
      //            out — and the error is swallowed rather than shown.
      // Best-effort: the server writes an audit-log line, but the JWT is
      // stateless, so the client-side clear below is what actually signs out.
      if (token) {
        try {
          await api.authLogout(token);
        } catch (err: unknown) {
          console.warn('Server-side logout call failed; clearing session locally', err);
        }
      }
    } finally {
      // AUTH 4.3 — clearSession runs even when the server call threw, so the UI
      //            can never stay stuck in a half-signed-out state.
      clearSession();
      setIsAuthenticating(false);
      notify('Signed out', 'info');
    }
  }, [clearSession, token]);

  const deleteAccount = useCallback(async (password: string, confirmation: string) => {
    if (!token) return 'Your session is no longer active. Please sign in again.';
    setIsAuthenticating(true);
    try {
      await api.authDeleteAccount(password, confirmation, token);
      clearSession();
      notify('Account and owned data deleted permanently', 'success');
      return null;
    } catch (err: unknown) {
      return detailFromJsonError(err) ?? 'Account deletion failed. Nothing was deleted.';
    } finally {
      setIsAuthenticating(false);
    }
  }, [clearSession, token]);

  return {
    user,
    token,
    isAuthenticating,
    signupEnabled,
    login,
    register,
    logout,
    deleteAccount,
    isAuthModalOpen,
    setIsAuthModalOpen,
  };
}
