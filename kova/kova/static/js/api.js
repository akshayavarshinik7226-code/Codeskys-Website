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
  // Multipart upload with a progress callback (fetch cannot report upload progress).
  function upload(path, form, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", path);
      xhr.setRequestHeader("Accept", "application/json");
      xhr.setRequestHeader("X-CSRF-Token", csrf);
      xhr.withCredentials = true;
      xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
      xhr.onerror = () => reject(new Error("Network error. Check your connection and try again."));
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (_) {}
        if (data && data.csrf) csrf = data.csrf;
        if (xhr.status >= 200 && xhr.status < 300) return resolve(data);
        if (xhr.status === 401) { window.location.assign("/login"); return reject(new Error("Your session has expired.")); }
        const err = new Error((data && data.error) || (xhr.status === 413 ? "That file is too large." : "Upload failed. Please try again."));
        err.status = xhr.status; err.data = data; reject(err);
      };
      xhr.send(form);
    });
  }
  return {
    upload,
    get: (p) => request("GET", p),
    post: (p, b) => request("POST", p, b === undefined ? {} : b),
    patch: (p, b) => request("PATCH", p, b || {}),
    del: (p, b) => request("DELETE", p, b || {}),
    init: () => request("GET", "/api/auth/me"),
  };
})();
