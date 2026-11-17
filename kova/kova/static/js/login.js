(function () {
  const form = document.getElementById("login-form");
  const verifyForm = document.getElementById("verify-form");
  const error = document.getElementById("login-error");
  const button = document.getElementById("login-btn");
  const title = document.getElementById("auth-title");
  const subtitle = document.getElementById("auth-subtitle");
  const nameField = document.getElementById("name-field");
  const nameInput = document.getElementById("name");
  const emailInput = document.getElementById("email");
  const password = document.getElementById("password");
  const passwordHint = document.getElementById("password-hint");
  const loginTab = document.getElementById("login-tab");
  const registerTab = document.getElementById("register-tab");
  const authSwitch = document.getElementById("auth-switch");
  const tabs = document.querySelector(".auth-tabs");
  const finePrint = document.querySelector(".fine");
  const extras = document.getElementById("pw-extras");
  let mode = "login";
  let pendingEmail = "";

  // Show / hide eye on the password box, plus (when creating an account) a strength check
  // and a "suggest a strong password" button.
  const eyeWrap = PwTools.eye(password);
  const meter = PwTools.meter(password);
  const suggest = PwTools.suggest([password], [eyeWrap]);
  extras.append(meter.node, suggest);
  extras.hidden = true;

  function setMode(next) {
    mode = next;
    const registering = mode === "register";
    title.textContent = registering ? "Create your Codeskys account" : "Welcome back";
    subtitle.textContent = registering ? "Create an account to start deploying your projects." : "Sign in to your Codeskys workspace.";
    nameField.classList.toggle("hidden", !registering);
    nameInput.required = registering;
    password.autocomplete = registering ? "new-password" : "current-password";
    password.placeholder = registering ? "Create a password" : "Enter your password";
    passwordHint.textContent = registering ? "Use at least 12 characters, with upper and lower case letters, a number and a symbol." : "Use your Codeskys account password.";
    extras.hidden = !registering;
    button.textContent = registering ? "Continue" : "Log In";
    loginTab.classList.toggle("active", !registering);
    registerTab.classList.toggle("active", registering);
    loginTab.setAttribute("aria-selected", String(!registering));
    registerTab.setAttribute("aria-selected", String(registering));
    authSwitch.innerHTML = registering ? 'Already have an account? <button type="button" id="switch-login">Log In</button>' : 'New to Codeskys? <button type="button" id="switch-register">Create an account</button>';
    document.getElementById(registering ? "switch-login" : "switch-register").addEventListener("click", () => setMode(registering ? "login" : "register"));
    error.textContent = "";
  }

  password.addEventListener("input", () => { if (mode === "register") error.textContent = ""; });
  loginTab.addEventListener("click", () => setMode("login"));
  registerTab.addEventListener("click", () => setMode("register"));
  document.getElementById("switch-register").addEventListener("click", () => setMode("register"));

  Api.init().then(({ user }) => { if (user) window.location.assign("/"); }).catch(() => {});

  // ---- Step 1: Log In, or send the sign-up details and get a code by email
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    const email = emailInput.value.trim();
    const pass = password.value;
    const name = nameInput.value.trim();
    if (!email || !pass || (mode === "register" && !name)) {
      error.textContent = mode === "register" ? "Enter your name, email address and password." : "Enter your email address and password.";
      return;
    }
    // Rules are checked when a password is CREATED. Logging in never checks them, so people
    // with an older password can always get in.
    if (mode === "register" && PwTools.score(pass).met < PwTools.RULES.length) {
      error.textContent = "Choose a stronger password. Use the checklist under the box, or tap \u201cSuggest a strong password\u201d.";
      return;
    }
    button.disabled = true;
    const label = mode === "register" ? "Continue" : "Log In";
    button.innerHTML = '<span class="spinner"></span> ' + (mode === "register" ? "Sending your code\u2026" : "Signing in\u2026");
    try {
      if (mode === "register") {
        const r = await Api.post("/api/auth/register", { name, email, password: pass });
        if (r.verify) { showVerify(r.email || email, r.resend_after || 30, Math.round((r.expires_in || 600) / 60)); button.disabled = false; button.textContent = label; return; }
      } else {
        await Api.post("/api/auth/login", { email, password: pass });
      }
      window.location.assign("/");
    } catch (err) {
      error.textContent = err.message;
      button.disabled = false;
      button.textContent = label;
    }
  });

  // ---- Step 2: the 6-digit code
  const otp = document.getElementById("otp");
  const verifyError = document.getElementById("verify-error");
  const verifyBtn = document.getElementById("verify-btn");
  const resendBtn = document.getElementById("resend-btn");
  const resendNote = document.getElementById("resend-note");
  let timer = null;

  function maskEmail(email) {
    const [local, domain] = email.split("@");
    return (local.length <= 2 ? local[0] + "*" : local[0] + "\u2022\u2022\u2022" + local.slice(-1)) + "@" + domain;
  }
  function showVerify(email, wait, minutes) {
    pendingEmail = email;
    form.classList.add("hidden");
    tabs.classList.add("hidden");
    authSwitch.classList.add("hidden");
    if (finePrint) finePrint.classList.add("hidden");
    title.classList.add("hidden"); subtitle.classList.add("hidden");
    verifyForm.classList.remove("hidden");
    document.getElementById("verify-sub").textContent = "We sent a 6-digit code to " + maskEmail(email) + ". It works for " + (minutes || 10) + " minutes.";
    otp.value = ""; verifyError.textContent = "";
    otp.focus();
    startCountdown(wait);
  }
  function startCountdown(seconds) {
    clearInterval(timer);
    let left = seconds;
    resendBtn.disabled = true;
    const tick = () => {
      if (left <= 0) { clearInterval(timer); resendBtn.disabled = false; resendBtn.textContent = "Resend code"; resendNote.textContent = "Didn\u2019t get it?"; return; }
      resendNote.textContent = "You can ask for a new code in " + left + "s.";
      resendBtn.textContent = "";
      left -= 1;
    };
    tick();
    timer = setInterval(tick, 1000);
  }
  otp.addEventListener("input", () => { otp.value = otp.value.replace(/\D/g, "").slice(0, 6); });

  verifyForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    verifyError.textContent = "";
    if (otp.value.length !== 6) { verifyError.textContent = "Enter the 6-digit code from your email."; return; }
    verifyBtn.disabled = true;
    verifyBtn.innerHTML = '<span class="spinner"></span> Checking\u2026';
    try {
      await Api.post("/api/auth/register/verify", { email: pendingEmail, code: otp.value });
      window.location.assign("/");
    } catch (err) {
      verifyError.textContent = err.message;
      verifyBtn.disabled = false;
      verifyBtn.textContent = "Verify and create account";
      if (err.status === 404 || err.status === 410 || err.status === 429) { /* sign-up ended: offer a fresh start */ otp.value = ""; }
    }
  });

  resendBtn.addEventListener("click", async () => {
    verifyError.textContent = "";
    resendBtn.disabled = true;
    try {
      const r = await Api.post("/api/auth/register/resend", { email: pendingEmail });
      verifyError.textContent = "";
      resendNote.textContent = "A new code is on its way.";
      startCountdown(r.resend_after || 30);
      otp.value = ""; otp.focus();
    } catch (err) {
      verifyError.textContent = err.message;
      if (err.data && err.data.retry_after) startCountdown(err.data.retry_after); else resendBtn.disabled = false;
    }
  });

  document.getElementById("verify-back").addEventListener("click", () => {
    clearInterval(timer);
    verifyForm.classList.add("hidden");
    form.classList.remove("hidden"); tabs.classList.remove("hidden"); authSwitch.classList.remove("hidden");
    if (finePrint) finePrint.classList.remove("hidden");
    title.classList.remove("hidden"); subtitle.classList.remove("hidden");
    emailInput.focus();
  });
})();
