export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export async function api<T>(path: string, opts: RequestInit = {}): Promise<T> {
  let res: Response;
  try { res = await fetch("/api" + path, opts); }
  catch { throw new ApiError("Can't reach the server", 0); }
  if (!res.ok) {
    // FastAPI puts a readable reason in `detail` (a string for our own errors).
    const detail = await res.json().then(b => b?.detail, () => null);
    const msg = typeof detail === "string" ? detail : res.status === 404 ? "Not found" : `Request failed (${res.status})`;
    throw new ApiError(msg, res.status);
  }
  return res.json();
}

export const post = <T>(path: string, body?: unknown, method = "POST") => api<T>(path, {
  method, headers: body === undefined ? undefined : { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});
