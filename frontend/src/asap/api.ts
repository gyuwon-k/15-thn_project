export class ApiError extends Error {
  reason: string; action: string;
  constructor(message: string, reason = '', action = '') { super(message); this.reason = reason; this.action = action; }
}
export async function api<T = any>(url: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try { response = await fetch('/api/asap' + url, options); }
  catch { throw new ApiError('로컬 서버에 연결할 수 없습니다. 서버 실행 상태를 확인하세요.', 'SERVER_UNREACHABLE'); }
  const raw = await response.text();
  let data: any;
  try { data = raw ? JSON.parse(raw) : {}; } catch { data = { message: raw.slice(0, 300) }; }
  if (!response.ok) {
    const detail = data.detail ?? data;
    const message = Array.isArray(detail) ? detail.map(item => `${item.loc?.slice(1).join('.') ?? '입력'}: ${item.msg}`).join(' · ') : typeof detail === 'string' ? detail : detail.message ?? `요청 실패 (${response.status})`;
    throw new ApiError(message, detail.reason_code, detail.action);
  }
  return data as T;
}
export function post<T = any>(url: string, data: unknown = {}): Promise<T> {
  return api<T>(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
}
