const progress = document.getElementById("project-progress");
const back = document.getElementById("project-back");
const buttons = [...document.querySelectorAll(".project-page button")];
const disabledBefore = buttons.map((button) => button.disabled);
let busy = false;

function showProgress(message) {
  if (busy) return false;
  busy = true;
  document.body.setAttribute("aria-busy", "true");
  progress.querySelector("[data-busy-label]").textContent = message;
  progress.classList.add("visible");
  buttons.forEach((button) => {
    button.disabled = true;
  });
  back.setAttribute("aria-disabled", "true");
  return true;
}

back.addEventListener("click", (event) => {
  if (
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey
  )
    return;
  event.preventDefault();
  if (!showProgress("Powrót do Importu…")) return;
  // Allow the waiting state to paint before the next Ingress request.
  window.setTimeout(() => window.location.assign(back.href), 50);
});

for (const form of document.querySelectorAll(
  ".project-page form[data-busy-message]",
)) {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!showProgress(form.dataset.busyMessage)) return;
    // Keep reviewed id/hash fields enabled and submit the original form once.
    window.setTimeout(() => form.submit(), 50);
  });
}

window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  busy = false;
  document.body.removeAttribute("aria-busy");
  progress.classList.remove("visible");
  buttons.forEach((button, index) => {
    button.disabled = disabledBefore[index];
  });
  back.removeAttribute("aria-disabled");
});
