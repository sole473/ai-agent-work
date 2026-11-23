const form = document.querySelector("#auth-form");
const status = document.querySelector("#auth-status");
const submit = document.querySelector("#submit-auth");
const loginTab = document.querySelector("#login-tab");
const registerTab = document.querySelector("#register-tab");
let mode = "login";

function setMode(next) {
  mode = next;
  submit.textContent = mode === "login" ? "登录" : "注册并登录";
  form.elements.password.autocomplete = mode === "login" ? "current-password" : "new-password";
  loginTab.classList.toggle("secondary", mode !== "login");
  registerTab.classList.toggle("secondary", mode === "login");
  status.textContent = "";
}

loginTab.addEventListener("click", () => setMode("login"));
registerTab.addEventListener("click", () => setMode("register"));

form.addEventListener("submit", async event => {
  event.preventDefault();
  submit.disabled = true;
  status.textContent = mode === "login" ? "正在登录…" : "正在创建账号…";
  try {
    const response = await fetch(`/auth/${mode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: form.elements.username.value, password: form.elements.password.value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "请求失败，请稍后重试。");
    location.assign("/");
  } catch (error) {
    status.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

fetch("/auth/me").then(response => response.json()).then(user => {
  if (user.id) location.assign("/");
}).catch(() => {});
