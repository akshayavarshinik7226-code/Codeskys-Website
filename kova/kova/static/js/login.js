(function () {
  const form = document.getElementById("login-form");
  const error = document.getElementById("login-error");
  const button = document.getElementById("login-btn");
  const title = document.getElementById("auth-title");
  const subtitle = document.getElementById("auth-subtitle");
  const nameField = document.getElementById("name-field");
  const nameInput = document.getElementById("name");
  const password = document.getElementById("password");
  const passwordHint = document.getElementById("password-hint");
  const loginTab = document.getElementById("login-tab");
  const registerTab = document.getElementById("register-tab");
  const switchRegister = document.getElementById("switch-register");
  const authSwitch = document.getElementById("auth-switch");
  let mode = "login";

  function setMode(next) {
    mode = next;
    const registering = mode === "register";
    title.textContent = registering ? "Create your Codeskys account" : "Welcome back";
    subtitle.textContent = registering ? "Create an account to start deploying your projects." : "Sign in to your Codeskys workspace.";
    nameField.classList.toggle("hidden", !registering);
    nameInput.required = registering;
    password.autocomplete = registering ? "new-password" : "current-password";
    password.placeholder = registering ? "Create a password" : "Enter your password";
    passwordHint.textContent = registering ? "Use at least 12 characters." : "Use your Codeskys account password.";
    button.textContent = registering ? "Create account" : "Log in";
    loginTab.classList.toggle("active", !registering);
    registerTab.classList.toggle("active", registering);
    loginTab.setAttribute("aria-selected", String(!registering));
    registerTab.setAttribute("aria-selected", String(registering));
    authSwitch.innerHTML = registering ? 'Already have an account? <button type="button" id="switch-login">Log in</button>' : 'New to Codeskys? <button type="button" id="switch-register">Create an account</button>';
    const switchButton = document.getElementById(registering ? "switch-login" : "switch-register");
    switchButton.addEventListener("click", () => setMode(registering ? "login" : "register"));
    error.textContent = "";
  }

  loginTab.addEventListener("click", () => setMode("login"));
  registerTab.addEventListener("click", () => setMode("register"));
  switchRegister.addEventListener("click", () => setMode("register"));

  Api.init().then(({ user }) => { if (user) window.location.assign("/"); }).catch(() => {});

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    const email = document.getElementById("email").value.trim();
    const pass = password.value;
    const name = nameInput.value.trim();
    if (!email || !pass || (mode === "register" && !name)) {
      error.textContent = mode === "register" ? "Enter your name, email address and password." : "Enter your email address and password.";
      return;
    }
    if (mode === "register" && pass.length < 12) {
      error.textContent = "Your password must be at least 12 characters.";
      return;
    }
    button.disabled = true;
    const label = mode === "register" ? "Create account" : "Log in";
    button.innerHTML = '<span class="spinner"></span> ' + (mode === "register" ? "Creating account…" : "Signing in…");
    try {
      if (mode === "register") await Api.post("/api/auth/register", { name, email, password: pass });
      else await Api.post("/api/auth/login", { email, password: pass });
      window.location.assign("/");
    } catch (err) {
      error.textContent = err.message;
      button.disabled = false;
      button.textContent = label;
    }
  });
})();
