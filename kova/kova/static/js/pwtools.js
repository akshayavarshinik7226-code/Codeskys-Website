/* Password helpers shared by the Log In page and Settings:
   a show/hide eye, a live strength check, and a "suggest a strong password" button.
   The rules here match the server's (12+ characters, upper + lower case, a number and a symbol);
   the server is the real gatekeeper, this only guides the person while typing. */
window.PwTools = (function () {
  const EYE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
  const EYE_OFF = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3l18 18"/><path d="M10.6 5.1A10.6 10.6 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.3 4.2M6.6 6.6A17 17 0 0 0 2 12s3.6 7 10 7a10 10 0 0 0 4.2-.9"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>';

  const RULES = [
    ["len", "12 or more characters", (p) => p.length >= 12],
    ["upper", "An uppercase letter (A-Z)", (p) => /[A-Z]/.test(p)],
    ["lower", "A lowercase letter (a-z)", (p) => /[a-z]/.test(p)],
    ["digit", "A number (0-9)", (p) => /\d/.test(p)],
    ["sym", "A symbol such as ! @ # $ %", (p) => /[^A-Za-z0-9]/.test(p)],
  ];

  function score(p) {
    if (!p) return { met: 0, level: 0, label: "" };
    const met = RULES.filter((r) => r[2](p)).length;
    let level = met <= 2 ? 1 : met <= 4 ? 2 : p.length >= 16 ? 4 : 3;
    return { met, level, label: ["", "Too weak", "Almost there", "Good", "Strong"][level] };
  }

  /* Wrap a password input with a show/hide eye. Returns the wrapper. */
  function eye(input) {
    const wrap = document.createElement("div");
    wrap.className = "pw-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.append(input);
    const btn = document.createElement("button");
    btn.type = "button"; btn.className = "pw-eye";
    btn.setAttribute("aria-label", "Show password"); btn.setAttribute("aria-pressed", "false");
    btn.title = "Show password"; btn.innerHTML = EYE;
    btn.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.innerHTML = show ? EYE_OFF : EYE;
      btn.setAttribute("aria-pressed", String(show));
      const label = show ? "Hide password" : "Show password";
      btn.setAttribute("aria-label", label); btn.title = label;
      input.focus();
    });
    wrap.append(btn);
    wrap._reveal = (on) => { if ((input.type === "text") !== on) btn.click(); };
    return wrap;
  }

  /* A live strength bar and rule checklist under an input. Returns {node, update}. */
  function meter(input) {
    const box = document.createElement("div");
    box.className = "pw-meter"; box.hidden = true;
    box.innerHTML = '<div class="pw-bars"><i></i><i></i><i></i><i></i></div><div class="pw-label" aria-live="polite"></div><ul class="pw-rules"></ul>';
    const ul = box.querySelector(".pw-rules");
    RULES.forEach((r) => { const li = document.createElement("li"); li.dataset.rule = r[0]; li.textContent = r[1]; ul.append(li); });
    function update() {
      const p = input.value, s = score(p);
      box.hidden = !p;
      box.dataset.level = String(s.level);
      box.querySelector(".pw-label").textContent = s.label;
      RULES.forEach((r) => ul.querySelector('[data-rule="' + r[0] + '"]').classList.toggle("ok", r[2](p)));
    }
    input.addEventListener("input", update);
    update();
    return { node: box, update };
  }

  /* A strong random password: every character class present, easy-to-read characters only. */
  function generate(length) {
    length = Math.max(14, length || 16);
    const sets = ["ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz", "23456789", "!@#$%^&*-_=+?"];
    const all = sets.join("");
    const rnd = (n) => { const a = new Uint32Array(1); const lim = Math.floor(4294967296 / n) * n; let x; do { crypto.getRandomValues(a); x = a[0]; } while (x >= lim); return x % n; };
    const out = sets.map((s) => s[rnd(s.length)]);
    while (out.length < length) out.push(all[rnd(all.length)]);
    for (let i = out.length - 1; i > 0; i--) { const j = rnd(i + 1); [out[i], out[j]] = [out[j], out[i]]; }
    return out.join("");
  }

  /* A "Suggest a strong password" button. `inputs` are filled (and revealed) when it is used. */
  function suggest(inputs, wraps) {
    const row = document.createElement("div");
    row.className = "pw-suggest";
    const b = document.createElement("button");
    b.type = "button"; b.className = "pw-suggest-btn";
    b.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l1.9 4.6L18.5 9l-4.6 1.4L12 15l-1.9-4.6L5.5 9l4.6-1.4z"/><path d="M19 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/></svg> Suggest a strong password';
    const note = document.createElement("span");
    note.className = "pw-note";
    b.addEventListener("click", async () => {
      const pw = generate(16);
      inputs.forEach((i) => { i.value = pw; i.dispatchEvent(new Event("input", { bubbles: true })); });
      (wraps || []).forEach((w) => w && w._reveal && w._reveal(true));
      let copied = false;
      try { await navigator.clipboard.writeText(pw); copied = true; } catch (_) {}
      note.textContent = copied ? "Filled in and copied. Save it somewhere safe." : "Filled in. Save it somewhere safe.";
    });
    row.append(b, note);
    return row;
  }

  return { eye, meter, suggest, generate, score, RULES };
})();
