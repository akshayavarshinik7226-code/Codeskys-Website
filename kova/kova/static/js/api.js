/* Codeskys API client. Keeps the CSRF token in memory and attaches it to writes. */
window.Api = (function () {
  let csrf = "";
  async function request(method, path, body) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET") headers["X-CSRF-Token"] = csrf;
    let res;
    try {
      res = await fetch(path, {
        method, headers, credentials: "same-origin",
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (_) {
      throw new Error("Network error. Check your connection and try again.");
    }
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (data && data.csrf) csrf = data.csrf;
    if (res.status === 401 && path !== "/api/auth/me") {
      window.location.assign("/login");
      throw new Error("Your session has expired.");
    }
    if (!res.ok) {
      const err = new Error((data && data.error) || "Something went wrong. Please try again.");
      err.status = res.status; err.data = data;
      throw err;
    }
    return data;
  }
  return {
    get: (p) => request("GET", p),
    post: (p, b) => request("POST", p, b === undefined ? {} : b),
    patch: (p, b) => request("PATCH", p, b || {}),
    del: (p, b) => request("DELETE", p, b || {}),
    init: () => request("GET", "/api/auth/me"),
  };
})();
