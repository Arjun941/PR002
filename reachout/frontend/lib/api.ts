export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export async function api<T>(path: string, opts: RequestInit = {}): Promise<T> {
  let res: Response;
  try { res = await fetch("/api" + path, opts); }
  catch { throw new ApiError("Can't reach the server", 0); }
  if (!res.ok) throw new ApiError(res.status === 404 ? "Not found" : `Request failed (${res.status})`, res.status);
  return res.json();
}
