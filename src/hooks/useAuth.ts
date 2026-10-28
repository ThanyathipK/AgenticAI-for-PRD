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

// AUTH 1.3 — Own the browser's authenticated session.
// Trigger: Dashboard mounts this hook once for the application.
// Input: login/register credentials and any JWT restored from localStorage.
// Calls: AUTH 1.4 login, AUTH 2.2 profile validation, AUTH 3.2 capability
//        probe, AUTH 4.2 logout, and AUTH 6.2 registration.
// Branches: valid session → authenticated UI; missing/invalid session → signed-out UI.
// Writes: axios Authorization header, localStorage, and React auth state.
// Failure: capability checks fail closed; stale sessions are cleared locally.
export function useAuth(): UseAuthResult {
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<AuthUserPayload | null>(null);
  const [isAuthenticating, setIsAuthenticating] = useState<boolean>(false);
  const [signupEnabled, setSignupEnabled] = useState<boolean>(false);
  const [isAuthModalOpen, setIsAuthModalOpen] = useState<boolean>(false);

  // AUTH 3.1 — Load public authentication capabilities.
  // Trigger: useAuth mount.
  // Calls: AUTH 3.2 api.authConfig → AUTH 3.3 GET /api/auth/config.
  // Writes: signupEnabled React state.
  // Failure: AUTH 3.4 fails closed; sign-up stays hidden when capability cannot
  //          be confirmed.
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

  // AUTH 2.1 — Restore and validate a persisted browser session.
  // Trigger: useAuth mount when localStorage contains a JWT.
  // Input: persisted JWT from AUTH 1.5.
  // Calls: AUTH 1.5.1 setAuthToken → AUTH 2.2 api.authMe → AUTH 2.3 route →
  //        AUTH 7.1 authorization dependency.
  // Branches: valid token/user → restore profile; rejected token/user → AUTH 2.4.
  // Writes: axios header and React token/user state.
  // Failure: AUTH 2.4 clears every local session sink.
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

  // AUTH 4.3 — Clear the authoritative browser session.
  // Trigger: logout or rejection of a restored/deleted session.
  // Writes: removes the axios header, localStorage JWT, and React token/user state.
  // Failure: none; this local operation is the effective sign-out because JWTs
  //          are stateless.
  // Next: AUTH 1.6 signed-out UI.
  const clearSession = useCallback(() => {
    setAuthToken(null);
    writeStoredToken(null);
    setToken(null);
    setUser(null);
  }, []);

  // AUTH 1.3.1 — Authenticate credentials and establish the browser session.
  // Trigger: AUTH 1.2 AuthModal sign-in or AUTH 6.4 post-registration login.
  // Input: trimmed email and plaintext password.
  // Calls: AUTH 1.4 API client → AUTH 1.7 route → AUTH 1.7.1 service.
  // Branches: success → AUTH 1.5 persistence; rejection → inline error string.
  // Writes: authentication progress plus token/user state on success.
  // Failure: unknown email and wrong password intentionally surface the same 401.
  const login = useCallback(async (email: string, password: string) => {
    setIsAuthenticating(true);
    try {
      const session = await api.authLogin(email.trim(), password);
      // AUTH 1.5 — Persist an authenticated session in all browser sinks.
      // Trigger: successful AUTH 1.3.1 login response.
      // Writes: AUTH 1.5.1 axios header → localStorage → React token/user state.
      // Next: AUTH 1.6 authenticated UI; localStorage later feeds AUTH 2.1.
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

  // AUTH 6.1 — Register a new account and continue into normal login.
  // Trigger: AUTH 1.2 AuthModal sign-up submission.
  // Input: email, password, full name, and optional role.
  // Calls: AUTH 6.2 api.authRegister → AUTH 6.3 route.
  // Branches: registration error → inline message; success → AUTH 6.4.
  // Writes: no local session until the subsequent login succeeds.
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

      // AUTH 6.4 — Enter the normal login flow after account creation.
      // Trigger: AUTH 6.3.6 returns a created user without a token.
      // Calls: AUTH 1.3.1 with the credentials just registered.
      // Branches: login success → authenticated UI; login failure → account-created
      //           notice plus a request to sign in manually.
      const signInError = await login(email, password);
      if (signInError) {
        notify('Account created. Please sign in.', 'success');
        return signInError;
      }
      return null;
    },
    [login],
  );

  // AUTH 4.1 — Sign out the current browser session.
  // Trigger: AuthPanel logout action.
  // Input: current JWT, when present.
  // Calls: AUTH 4.3 local clear, then AUTH 4.2 best-effort server audit request.
  // Writes: clears browser auth state immediately; server route writes no session state.
  // Failure: server-audit failure is logged and ignored; local sign-out still completes.
  // Next: AUTH 1.6 signed-out UI.
  const logout = useCallback(async () => {
    setIsAuthenticating(true);
    // Clear the browser session before the best-effort audit request. Besides
    // making logout feel immediate, this prevents any project UI from staying
    // visible while a slow/unreachable backend handles POST /auth/logout.
    const tokenToLogout = token;
    clearSession();
    try {
      // AUTH 4.2 — Notify the server of logout for audit logging.
      // Trigger: AUTH 4.1 after the browser session has already been cleared.
      // Calls: API client → AUTH 4.4 POST /api/auth/logout.
      // Writes: no server-side session mutation because the JWT is stateless.
      // Failure: fail softly; log and keep the completed local sign-out.
      // Best-effort: the server writes an audit-log line, but the JWT is
      // stateless, so the client-side clear below is what actually signs out.
      if (tokenToLogout) {
        try {
          await api.authLogout(tokenToLogout);
        } catch (err: unknown) {
          console.warn('Server-side logout call failed; clearing session locally', err);
        }
      }
    } finally {
      // AUTH 4.3 — clearSession runs even when the server call threw, so the UI
      //            can never stay stuck in a half-signed-out state.
      setIsAuthenticating(false);
      notify('Signed out', 'info');
    }
  }, [clearSession, token]);

  // AUTH 8.2 — Request account deletion and retire the browser session.
  // Trigger: AUTH 8.1 confirmed modal submission.
  // Input: current password, DELETE confirmation, and current JWT.
  // Calls: AUTH 8.3 api.authDeleteAccount → AUTH 8.4 backend route.
  // Branches: success → AUTH 8.7 clear; failure → inline error with session retained.
  // Writes: authentication progress and, on success, all local session sinks.
  // Failure: missing token stops before the request; route errors remain user-visible.
  const deleteAccount = useCallback(async (password: string, confirmation: string) => {
    if (!token) return 'Your session is no longer active. Please sign in again.';
    setIsAuthenticating(true);
    try {
      await api.authDeleteAccount(password, confirmation, token);
      // AUTH 8.7 — Clear the now-deleted account's browser session.
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
