// ============================================================================
// Auth wiring tests — the client half of backend/app/routes/auth.py.
//
//   src/api/client.ts    authLogin / authRegister / authConfig / authMe /
//                        authLogout, setAuthToken (axios stubbed)
//   src/hooks/useAuth.ts authInitials, readStoredToken / writeStoredToken
//                        (localStorage absent -> no-op, as in this Node run)
//
// The request *contract* matters more than the plumbing here: the backend reads
// login as OAuth2PasswordRequestForm (form-encoded username/password, NOT JSON)
// and /me, /logout as Bearer tokens. If either drifts, sign-in fails at runtime
// with an opaque 422/401 — so both are asserted directly.
//
// Run:  npx tsx scripts/auth.test.ts
// ============================================================================
import assert from 'node:assert/strict';
import { test } from 'node:test';
import axios from 'axios';

import { api, setAuthToken } from '../src/api/client';
import {
  AUTH_TOKEN_STORAGE_KEY,
  authInitials,
  readStoredToken,
  writeStoredToken,
} from '../src/hooks/useAuth';

// ---- axios stubbing helpers ------------------------------------------------
type Call = { url: string; body: unknown; config: Record<string, unknown> };

/** Replace one axios verb, capturing the call and returning `data`. */
function stubPost(response: unknown): Call[] {
  const calls: Call[] = [];
  (axios as unknown as { post: unknown }).post = (async (
    url: string,
    body: unknown,
    config: Record<string, unknown> = {},
  ) => {
    calls.push({ url, body, config });
    return { data: response };
  }) as never;
  return calls;
}

function stubGet(response: unknown): Call[] {
  const calls: Call[] = [];
  (axios as unknown as { get: unknown }).get = (async (
    url: string,
    config: Record<string, unknown> = {},
  ) => {
    calls.push({ url, body: null, config });
    return { data: response };
  }) as never;
  return calls;
}

const originalPost = axios.post;
const originalGet = axios.get;

test.after(() => {
  axios.post = originalPost;
  axios.get = originalGet;
  setAuthToken(null);
});

// ---- POST /api/auth/login --------------------------------------------------
test('authLogin posts form-encoded username/password (OAuth2 password flow)', async () => {
  const calls = stubPost({
    access_token: 'jwt-token',
    token_type: 'bearer',
    user: { id: 'u1', email: 'ba@bank.com', full_name: 'Bank BA', role: 'Business Analyst', is_active: true },
  });

  const session = await api.authLogin('Ba@Bank.com', 'Correct-Horse-1');

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/auth/login');
  const body = calls[0].body as URLSearchParams;
  // The Pydantic form model expects these exact field names (trimming happens
  // in useAuth; the backend matches the email case-insensitively itself).
  assert.equal(body.get('username'), 'Ba@Bank.com');
  assert.equal(body.get('password'), 'Correct-Horse-1');
  assert.equal(session.access_token, 'jwt-token');
  assert.equal(session.user.email, 'ba@bank.com');
});

// ---- POST /api/auth/register ----------------------------------------------
test('authRegister posts the JSON RegisterRequest body', async () => {
  const calls = stubPost({ message: 'User registered successfully', user: { id: 'u2' } });

  await api.authRegister({
    email: 'new@bank.com',
    password: 'Super-Secret-9',
    full_name: 'New User',
    role: 'Project Manager',
  });

  assert.equal(calls[0].url, '/api/auth/register');
  assert.deepEqual(calls[0].body, {
    email: 'new@bank.com',
    password: 'Super-Secret-9',
    full_name: 'New User',
    role: 'Project Manager',
  });
});

// ---- GET /api/auth/me / POST /api/auth/logout ------------------------------
test('authMe and authLogout send the Bearer token the backend verifies', async () => {
  const getCalls = stubGet({ id: 'u1', email: 'ba@bank.com' });
  await api.authMe('jwt-token');
  assert.equal((getCalls[0].config.headers as Record<string, string>).Authorization, 'Bearer jwt-token');

  const postCalls = stubPost({ message: 'Successfully logged out' });
  const resp = await api.authLogout('jwt-token');
  assert.equal(postCalls[0].url, '/api/auth/logout');
  assert.equal((postCalls[0].config.headers as Record<string, string>).Authorization, 'Bearer jwt-token');
  assert.equal(resp.message, 'Successfully logged out');
});

// ---- setAuthToken ----------------------------------------------------------
test('setAuthToken mirrors the session into axios defaults and clears cleanly', () => {
  setAuthToken('abc123');
  assert.equal(axios.defaults.headers.common.Authorization, 'Bearer abc123');

  // Logout must leave nothing behind, or later anonymous calls keep the header.
  setAuthToken(null);
  assert.equal(axios.defaults.headers.common.Authorization, undefined);
});

// ---- session persistence ---------------------------------------------------
test('token storage accessors are safe when localStorage does not exist', () => {
  // This Node run has no `window`, which is exactly the SSR/private-mode case:
  // reading returns null and writing is a silent no-op instead of a crash.
  assert.equal(typeof window, 'undefined');
  assert.equal(readStoredToken(), null);
  writeStoredToken('jwt-token');
  assert.equal(readStoredToken(), null);
  writeStoredToken(null);
});

test('token storage round-trips through an injected localStorage stub', () => {
  const store = new Map<string, string>();
  const fakeStorage = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  };
  (globalThis as unknown as { window: unknown }).window = { localStorage: fakeStorage };

  try {
    writeStoredToken('jwt-token');
    assert.equal(store.get(AUTH_TOKEN_STORAGE_KEY), 'jwt-token');
    assert.equal(readStoredToken(), 'jwt-token');

    writeStoredToken(null);
    assert.equal(store.has(AUTH_TOKEN_STORAGE_KEY), false);
    assert.equal(readStoredToken(), null);
  } finally {
    delete (globalThis as unknown as { window?: unknown }).window;
  }
});

// ---- authInitials ----------------------------------------------------------
test('authInitials derives the sidebar avatar initials', () => {
  assert.equal(authInitials({ full_name: 'Ada Lovelace', email: 'ada@bank.com' }), 'AL');
  assert.equal(authInitials({ full_name: '  System   User  ', email: 's@bank.com' }), 'SU');
  assert.equal(authInitials({ full_name: 'Prince', email: 'prince@bank.com' }), 'PR');
  // Falls back to the email local part when the name is blank.
  assert.equal(authInitials({ full_name: '', email: 'ba@bank.com' }), 'BA');
  assert.equal(authInitials({ full_name: '', email: '' }), '?');
});

// ---- GET /api/auth/config --------------------------------------------------
test('authConfig reads the public capabilities probe without a token', async () => {
  const calls = stubGet({ signup_enabled: true, token_expiration_minutes: 60 });

  const config = await api.authConfig();

  assert.equal(calls[0].url, '/api/auth/config');
  assert.equal((calls[0].config.headers as Record<string, string>).Authorization, undefined);
  assert.equal(config.signup_enabled, true);
  assert.equal(config.token_expiration_minutes, 60);
});
