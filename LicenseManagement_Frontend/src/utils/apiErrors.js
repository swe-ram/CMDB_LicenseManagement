// Extract a safe, human-readable message from an API error. FastAPI returns a
// JSON `detail` string for HTTP errors and an array of objects for 422
// validation failures; anything else falls back to the provided default so the
// UI never renders raw payloads or crashes on non-string details.
export const formatApiError = (err, fallback) => {
  const detail = err?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (typeof item?.msg === 'string' ? item.msg : ''))
      .filter(Boolean);
    if (messages.length) return messages.join(' ');
  }
  return fallback;
};
