// useLmStudioHealth — Finding #40 health polling hook.
//
// The app previously assumed LM Studio was running. This hook continuously polls
// `GET /api/health` (server-side TTL-cached probe of LM Studio's /models endpoint)
// so the UI can surface a clear offline banner + local fallback message instead of
// surprising the user with opaque 503 errors. It refreshes on mount, on a timer,
// and whenever the tab regains focus so an LLM that comes back online is detected
// promptly.
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';

export interface LmStudioHealthState {
  /** Whether the FastAPI backend itself responded to the health poll (null = first check pending). */
  backendOnline: boolean | null;
  /** LM Studio /models probe result (null = unknown / first check pending). */
  lmStudioOnline: boolean | null;
  /** Configured LM_STUDIO_MODEL_FALLBACK id. */
  lmStudioModel: string | null;
  /** True when the configured model is currently served by LM Studio. */
  lmStudioModelLoaded: boolean;
  /** All model ids currently served by the local gateway (may be empty while offline). */
  lmStudioLoadedModels: string[];
  lmStudioLatencyMs: number | null;
  lmStudioLastCheckedAt: string | null;
  lmStudioError: string | null;
  /** True while a health request is in flight (normally only the very first check). */
  checkingLmStudio: boolean;
}

export interface LmStudioHealth extends LmStudioHealthState {
  /** Manually re-run the health check (e.g. a "Retry" button on the offline banner). */
  checkLmStudioHealth: () => Promise<void>;
}

// CHAT 6.3 — Frontend readiness path for the local inference gateway.
// Calls: CHAT 6.3.1 API-client read → CHAT 6.3.2 polling/state owner.
// Writes: readiness state only; no model or backend configuration changes.
const HEALTH_POLL_INTERVAL_MS = 20_000;

const INITIAL_STATE: LmStudioHealthState = {
  backendOnline: null,
  lmStudioOnline: null,
  lmStudioModel: null,
  lmStudioModelLoaded: false,
  lmStudioLoadedModels: [],
  lmStudioLatencyMs: null,
  lmStudioLastCheckedAt: null,
  lmStudioError: null,
  checkingLmStudio: true,
};

// CHAT 6.3.2 — Maintain frontend inference-readiness state.
// Trigger: hook mount, 20-second interval, tab focus, or manual retry.
// Calls: CHAT 6.3.1 api.fetchHealth → CHAT 6.2.1 health route.
// Writes: backend/LM Studio readiness React state.
// Branches: successful payload → model status; request failure → backend offline.
// Failure: fails softly into visible offline state rather than raising to the UI.
export function useLmStudioHealth(): LmStudioHealth {
  const [state, setState] = useState<LmStudioHealthState>(INITIAL_STATE);
  const inFlightRef = useRef(false);

  const checkHealth = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setState(prev => ({ ...prev, checkingLmStudio: true }));
    try {
      const data = await api.fetchHealth();
      setState({
        backendOnline: true,
        lmStudioOnline: Boolean(data.lm_studio_online),
        lmStudioModel: data.lm_studio_model ?? null,
        lmStudioModelLoaded: Boolean(data.lm_studio_model_loaded),
        lmStudioLoadedModels: Array.isArray(data.lm_studio_loaded_models)
          ? data.lm_studio_loaded_models
          : [],
        lmStudioLatencyMs:
          typeof data.lm_studio_latency_ms === 'number' ? data.lm_studio_latency_ms : null,
        lmStudioLastCheckedAt: data.lm_studio_last_checked ?? null,
        lmStudioError: data.lm_studio_error ?? null,
        checkingLmStudio: false,
      });
    } catch {
      // Backend unreachable — keep the last known LM Studio answer, flag the backend.
      setState(prev => ({
        ...prev,
        backendOnline: false,
        checkingLmStudio: false,
      }));
    } finally {
      inFlightRef.current = false;
    }
  }, []);

  useEffect(() => {
    checkHealth();
    const timer = window.setInterval(checkHealth, HEALTH_POLL_INTERVAL_MS);
    const onFocus = () => checkHealth();
    const onVisibility = () => {
      if (document.visibilityState === 'visible') checkHealth();
    };
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [checkHealth]);

  return { ...state, checkLmStudioHealth: checkHealth };
}
