// AuthModal — the sign-in / sign-up dialog opened from the sidebar's
// bottom-left user chip.
//
// Two modes share one card: "Sign in" (POST /api/auth/login, OAuth2 password
// flow) and "Create account" (POST /api/auth/register). The second tab only
// appears when the backend reports SIGNUP_ENABLED (GET /api/auth/config), so a
// deployment with self-service signup disabled never shows a dead form.
//
// Matching the app's design language (NewProjectModal / ConfirmModal): rounded-2xl
// card on a blurred overlay, outline-border inputs, primary CTA, Escape-to-close
// with a focus trap, and errors rendered inline (never via alert()).
import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, LogIn, ShieldCheck, UserPlus } from 'lucide-react';
import { useModalBehavior } from '../hooks/useModalBehavior';
import { AUTH_ROLES } from '../api/types';

type AuthMode = 'signin' | 'signup';

/** Mirrors backend/app/routes/auth.py MIN_PASSWORD_LENGTH. */
const MIN_PASSWORD_LENGTH = 8;

interface AuthModalProps {
  isOpen: boolean;
  /** Whether POST /api/auth/register is enabled server-side (SIGNUP_ENABLED). */
  signupEnabled: boolean;
  isAuthenticating: boolean;
  /** Resolve to null on success, or to a user-facing error message. */
  onSignIn: (email: string, password: string) => Promise<string | null>;
  onSignUp: (
    email: string,
    password: string,
    fullName: string,
    role: string,
  ) => Promise<string | null>;
  onClose: () => void;
}

// AUTH 1.1.1 — Present the sign-in and optional sign-up dialog.
// Trigger: AUTH 1.1 sign-in action opens the modal from Dashboard.
// Input: capability state plus login/register callbacks owned by useAuth.
// Branches: sign-in → AUTH 1.2.1; enabled sign-up → AUTH 1.2.2.
// Writes: local form/error state only.
// Failure: callback errors remain visible inline; the modal stays open.
export function AuthModal({
  isOpen,
  signupEnabled,
  isAuthenticating,
  onSignIn,
  onSignUp,
  onClose,
}: AuthModalProps) {
  const [mode, setMode] = useState<AuthMode>('signin');
  const [email, setEmail] = useState<string>('');
  const [password, setPassword] = useState<string>('');
  const [fullName, setFullName] = useState<string>('');
  const [role, setRole] = useState<string>(AUTH_ROLES[0]);
  const [error, setError] = useState<string | null>(null);
  const emailRef = useRef<HTMLInputElement>(null);

  // Shared modal behavior: Escape-anywhere-to-close, focus trap, focus restore
  // on close, background scroll lock. Escape stays disabled while a request is
  // in flight (same guard as the overlay click below).
  const { dialogRef } = useModalBehavior({
    isOpen,
    onClose: () => {
      if (!isAuthenticating) onClose();
    },
  });

  // Fresh, focused form every time the modal opens (always starting on the
  // sign-in tab, even if the user last used the sign-up one).
  useEffect(() => {
    if (!isOpen) return;
    setMode('signin');
    setEmail('');
    setPassword('');
    setFullName('');
    setRole(AUTH_ROLES[0]);
    setError(null);
    const timer = setTimeout(() => emailRef.current?.focus(), 30);
    return () => clearTimeout(timer);
  }, [isOpen, signupEnabled]);

  if (!isOpen) return null;

  const isSignUp = mode === 'signup';
  const passwordTooShort = isSignUp && password.length > 0 && password.length < MIN_PASSWORD_LENGTH;
  const canSubmit =
    email.trim().length > 0 &&
    password.length > 0 &&
    (!isSignUp || (fullName.trim().length > 0 && password.length >= MIN_PASSWORD_LENGTH)) &&
    !isAuthenticating;

  // AUTH 1.2 — Validate and dispatch authentication form submission.
  // Trigger: AuthModal form submit.
  // Input: selected mode and validated form fields.
  // Calls: AUTH 1.3.1 login or AUTH 6.1 registration callback.
  // Branches: error string → render inline; null → close modal.
  // Writes: local error state only; useAuth owns session state.
  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    setError(null);
    // AUTH 1.2.1 — Sign-in branch: Frontend → useAuth.login → api.authLogin
    //              → POST /api/auth/login (AUTH 1.4 → AUTH 1.7).
    // AUTH 1.2.2 — Sign-up branch: Frontend → useAuth.register → api.authRegister
    //              → POST /api/auth/register (AUTH 6.2 → AUTH 6.3), then login.
    const failure = isSignUp
      ? await onSignUp(email, password, fullName, role)
      : await onSignIn(email, password);
    // AUTH 1.5 — On success (failure === null) the token is ALREADY persisted by
    //             useAuth (axios header + localStorage + state), so closing the
    //             modal reveals the authenticated sidebar (AUTH 1.6).
    if (failure) setError(failure);
    else onClose();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={() => { if (!isAuthenticating) onClose(); }}
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        className="w-[360px] max-w-[calc(100vw-1.5rem)] bg-white border border-outline rounded-2xl shadow-xl p-5 focus:outline-none"
        role="dialog"
        aria-modal="true"
        aria-label={isSignUp ? 'Create an account' : 'Sign in'}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2.5 mb-4">
          <div className="w-8 h-8 rounded-xl bg-primary/10 flex items-center justify-center shrink-0">
            {isSignUp ? <UserPlus className="w-4 h-4 text-primary" /> : <ShieldCheck className="w-4 h-4 text-primary" />}
          </div>
          <div className="min-w-0 flex-1">
            <h3 className="text-[13px] font-bold text-on-surface">
              {isSignUp ? 'Create Account' : 'Sign In'}
            </h3>
            <p className="text-[11px] text-on-surface-variant">
              {isSignUp
                ? 'Register to access the workspace'
                : 'Authenticate against the requirements workspace'}
            </p>
          </div>
        </div>
        {/* Mode switcher — hidden entirely when self-service signup is off. */}
        {signupEnabled && (
          <div className="flex items-center gap-1 p-0.5 mb-4 bg-primary/5 rounded-xl">
            {(['signin', 'signup'] as const).map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => {
                  setMode(value);
                  setError(null);
                }}
                disabled={isAuthenticating}
                aria-pressed={mode === value}
                className={`flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-[11.5px] font-bold transition-colors cursor-pointer disabled:cursor-not-allowed ${
                  mode === value
                    ? 'bg-white text-primary shadow-sm'
                    : 'text-on-surface-variant hover:text-on-surface'
                }`}
              >
                {value === 'signin' ? <LogIn className="w-3.5 h-3.5" /> : <UserPlus className="w-3.5 h-3.5" />}
                {value === 'signin' ? 'Sign In' : 'Create Account'}
              </button>
            ))}
          </div>
        )}

        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          {isSignUp && (
            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-semibold text-on-surface-variant">Full name</span>
              <input
                type="text"
                value={fullName}
                onChange={(e) => { setFullName(e.target.value); if (error) setError(null); }}
                placeholder="Ada Lovelace"
                autoComplete="name"
                disabled={isAuthenticating}
                className="w-full bg-white border border-outline rounded-xl px-3 py-2 text-xs text-on-surface placeholder:text-on-surface-variant/60 focus:outline-none focus:border-primary transition-colors disabled:opacity-60"
              />
            </label>
          )}

          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-semibold text-on-surface-variant">Email</span>
            <input
              ref={emailRef}
              type="email"
              value={email}
              onChange={(e) => { setEmail(e.target.value); if (error) setError(null); }}
              placeholder="you@bank.com"
              autoComplete="email"
              required
              disabled={isAuthenticating}
              aria-invalid={!!error}
              className={`w-full bg-white border rounded-xl px-3 py-2 text-xs text-on-surface placeholder:text-on-surface-variant/60 focus:outline-none transition-colors disabled:opacity-60 ${
                error ? 'border-red-400 focus:border-red-500' : 'border-outline focus:border-primary'
              }`}
            />
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-semibold text-on-surface-variant">Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => { setPassword(e.target.value); if (error) setError(null); }}
              placeholder={isSignUp ? `At least ${MIN_PASSWORD_LENGTH} characters` : 'Password'}
              autoComplete={isSignUp ? 'new-password' : 'current-password'}
              required
              disabled={isAuthenticating}
              aria-invalid={!!error}
              className={`w-full bg-white border rounded-xl px-3 py-2 text-xs text-on-surface placeholder:text-on-surface-variant/60 focus:outline-none transition-colors disabled:opacity-60 ${
                error ? 'border-red-400 focus:border-red-500' : 'border-outline focus:border-primary'
              }`}
            />
          </label>

          {isSignUp && (
            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-semibold text-on-surface-variant">Role</span>
              <select
                value={role}
                onChange={(e) => setRole(e.target.value)}
                disabled={isAuthenticating}
                className="w-full bg-white border border-outline rounded-xl px-3 py-2 text-xs text-on-surface focus:outline-none focus:border-primary transition-colors disabled:opacity-60"
              >
                {AUTH_ROLES.map((r) => (
                  <option key={r} value={r}>{r}</option>
                ))}
              </select>
            </label>
          )}

          {passwordTooShort && (
            <p className="text-[11px] font-medium text-amber-600 leading-snug">
              Password must be at least {MIN_PASSWORD_LENGTH} characters.
            </p>
          )}
          {error && (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-xl border border-red-300 bg-red-50 px-3 py-2"
            >
              <AlertTriangle className="w-3.5 h-3.5 text-red-500 shrink-0 mt-0.5" />
              <span className="text-[11px] font-medium text-red-900 leading-snug">{error}</span>
            </div>
          )}

          <div className="flex items-center justify-end gap-2 mt-1">
            <button
              type="button"
              onClick={() => { if (!isAuthenticating) onClose(); }}
              disabled={isAuthenticating}
              className="px-3 py-2 rounded-xl bg-primary/5 text-on-surface-variant hover:bg-primary/10 hover:text-on-surface transition-colors text-xs font-semibold cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!canSubmit}
              className="px-4 py-2 rounded-xl bg-primary text-on-primary hover:brightness-110 active:scale-[0.99] transition-all text-xs font-bold shadow-md shadow-primary/20 flex items-center gap-1.5 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:brightness-100"
            >
              {isSignUp ? <UserPlus className="w-3.5 h-3.5" /> : <LogIn className="w-3.5 h-3.5" />}
              {isAuthenticating
                ? (isSignUp ? 'Creating...' : 'Signing in...')
                : (isSignUp ? 'Create Account' : 'Sign In')}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
