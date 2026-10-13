/** Canonical backend logs are UTF-8 text; event arrays remain readable. */
export function logLines(value: unknown): string[] {
  if (typeof value === 'string') return value ? value.split(/\r?\n/) : [];
  if (!Array.isArray(value)) return [];
  return value.flatMap(item => {
    if (typeof item === 'string') return item.split(/\r?\n/);
    if (item && typeof item === 'object') {
      if (typeof item.message === 'string') return item.message.split(/\r?\n/);
      try { return [JSON.stringify(item)]; } catch { return ['[로그 항목을 표시할 수 없습니다]']; }
    }
    return [];
  });
}
