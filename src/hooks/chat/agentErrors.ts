import axios from "axios";

type HttpErrorShape = {
  code?: string;
  name?: string;
  response?: {
    status?: number;
    data?: { detail?: unknown };
    headers?: Record<string, unknown> & { get?: (key: string) => unknown };
  };
};

const errorShape = (error: unknown): HttpErrorShape =>
  error && typeof error === "object" ? (error as HttpErrorShape) : {};

export const isRateLimitError = (error: unknown): boolean =>
  errorShape(error).response?.status === 429;

export const isPayloadTooLargeError = (error: unknown): boolean =>
  errorShape(error).response?.status === 413;

export const isUnauthorizedError = (error: unknown): boolean =>
  errorShape(error).response?.status === 401;

export const isAbortError = (error: unknown): boolean =>
  axios.isCancel(error) ||
  errorShape(error).code === "ERR_CANCELED" ||
  errorShape(error).name === "AbortError";

export const extractErrorDetail = (error: unknown): string | null => {
  const detail = errorShape(error).response?.data?.detail;
  return typeof detail === "string" ? detail : null;
};

export const rateLimitedContent = (error: unknown): string => {
  const headers = errorShape(error).response?.headers;
  const raw =
    headers?.["retry-after"] ??
    headers?.["Retry-After"] ??
    (typeof headers?.get === "function"
      ? headers.get("retry-after")
      : undefined);
  const seconds = Number(raw);
  const wait =
    Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : null;
  const suffix =
    wait === null
      ? "A short time window will restore access."
      : `Please wait about ${wait} second(s) before continuing.`;
  return `Rate limit reached. Too many requests in a short window. ${suffix}`;
};

export const payloadTooLargeContent = (error: unknown): string => {
  const measured = extractErrorDetail(error);
  return [
    "Project too large for the current context budget.",
    "The project's requirements, user stories and acceptance criteria exceed the LLM context budget (`MAX_CONTEXT_TOKENS`). The request was rejected before any agent ran. Nothing was saved or changed.",
    "",
    "How to resolve:",
    "- Trim the project: remove or merge redundant user stories or acceptance criteria, or",
    "- Raise `MAX_CONTEXT_TOKENS` in `backend/.env` (e.g. `16384`) if the loaded LM Studio model supports a larger context window.",
    measured ? `\n${measured}` : "",
  ]
    .filter(Boolean)
    .join("\n");
};
