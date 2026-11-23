const form = document.querySelector("#auth-form");
const status = document.querySelector("#auth-status");
const submit = document.querySelector("#submit-auth");

form.addEventListener("submit", async event => {
  event.preventDefault();
  submit.disabled = true;
  status.textContent = "正在登录…";
  try {
    const response = await fetch("/auth/login", {
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
