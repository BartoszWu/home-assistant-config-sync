function pageIsBusy() {
  return document.body.dataset.busy === "true";
}

function markPageBusy(message) {
  if (pageIsBusy()) return false;
  document.body.dataset.busy = "true";
  document.body.setAttribute("aria-busy", "true");
  const progress = document.getElementById("refresh-progress");
  if (progress) {
    const label = progress.querySelector("[data-busy-label]");
    if (label && message) label.textContent = message;
    progress.classList.add("visible");
  }
  [
    "refresh-button",
    "refresh-source-button",
    "apply-button",
    "cancel-button",
    "select-all-ready",
  ].forEach((id) => {
    const button = document.getElementById(id);
    if (button) button.disabled = true;
  });
  const refreshButton = document.getElementById("refresh-button");
  const sourceButton = document.getElementById("refresh-source-button");
  if (refreshButton) refreshButton.textContent = "Sprawdzanie…";
  if (sourceButton) sourceButton.textContent = "Sprawdzanie…";
  return true;
}

function submitAfterPaint(form) {
  window.setTimeout(() => form.submit(), 50);
}

function bindBusySubmit(form, message) {
  if (!form || form.dataset.busyBound) return;
  form.dataset.busyBound = "true";
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!markPageBusy(message)) return;
    submitAfterPaint(form);
  });
}

bindBusySubmit(
  document.getElementById("refresh-github-form"),
  "Sprawdzanie zmian…",
);
bindBusySubmit(document.getElementById("source-form"), "Sprawdzanie źródła…");

function bindReviewControls() {
  bindBusySubmit(document.getElementById("source-form"), "Sprawdzanie źródła…");
  const form = document.getElementById("apply-form");
  if (form && !form.dataset.selectionBound) {
    form.dataset.selectionBound = "true";
    const selectable = [
      ...form.querySelectorAll("input[data-bulk-selectable]"),
    ];
    const selectionInputs = [
      ...form.querySelectorAll(
        'input[name="selected"], input[name="managed_selected"], input[name="resource_selected"]',
      ),
    ];
    const bulkSelect = document.getElementById("bulk-select");
    const selectAll = document.getElementById("select-all-ready");
    const selectionCount = document.getElementById("selection-count");
    function updateSelection() {
      const chosen = selectionInputs.filter((input) => input.checked).length;
      form
        .querySelector(".actions")
        ?.classList.toggle("has-selection", chosen > 0);
      const apply = document.getElementById("apply-button");
      if (apply) apply.disabled = !chosen || pageIsBusy();
      if (selectAll) {
        const checked = selectable.filter((input) => input.checked).length;
        selectAll.checked =
          selectable.length > 0 && checked === selectable.length;
        selectAll.indeterminate = checked > 0 && checked < selectable.length;
        selectAll.disabled = selectable.length === 0 || pageIsBusy();
      }
      if (selectionCount) {
        const checked = selectionInputs.filter((input) => input.checked).length;
        selectionCount.textContent = `${checked} zaznaczonych`;
      }
    }
    if (selectAll) {
      selectAll.addEventListener("change", () => {
        selectable.forEach((input) => {
          input.checked = selectAll.checked;
        });
        updateSelection();
      });
    }
    selectionInputs.forEach((input) =>
      input.addEventListener("change", updateSelection),
    );
    form.addEventListener("reset", () => window.setTimeout(updateSelection, 0));
    updateSelection();
    if (bulkSelect) bulkSelect.hidden = false;
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (form.dataset.submitting === "true" || pageIsBusy()) return;
      const selected = form.querySelectorAll(
        'input[name="selected"]:checked, input[name="managed_selected"]:checked, input[name="resource_selected"]:checked',
      );
      if (!selected.length) {
        window.alert(
          "Zaznacz przynajmniej jeden element konfiguracji gotowy do wgrania.",
        );
        return;
      }
      form.dataset.submitting = "true";
      document.body.dataset.busy = "true";
      document.body.setAttribute("aria-busy", "true");
      const applyButton = document.getElementById("apply-button");
      applyButton.disabled = true;
      applyButton.textContent = "Wgrywanie…";
      [
        "refresh-button",
        "refresh-source-button",
        "cancel-button",
        "select-all-ready",
      ].forEach((id) => {
        const button = document.getElementById(id);
        if (button) button.disabled = true;
      });
      document.getElementById("apply-progress").classList.add("visible");
      window.setTimeout(() => form.submit(), 50);
    });
  }
  const sourceSelect = document.querySelector('#source select[name="source"]');
  if (sourceSelect && !sourceSelect.dataset.bound) {
    sourceSelect.dataset.bound = "true";
    sourceSelect.addEventListener("change", (event) => {
      const sourceForm = event.target.form;
      const sha = sourceForm.querySelector('input[name="source_sha"]');
      if (sha) sha.value = "";
      if (!markPageBusy("Sprawdzanie źródła…")) return;
      submitAfterPaint(sourceForm);
    });
  }
}
bindReviewControls();

const streamURL = document.body.dataset.reviewStream;
if (streamURL) {
  const stream = new EventSource(streamURL);
  const remaining = new Set(["applications", "configuration"]);
  function failReview(message) {
    stream.close();
    const title = document.getElementById("overview-title");
    if (title) title.textContent = "Nie wszystko udało się sprawdzić";
    const status = document.getElementById("review-loading-status");
    if (status)
      status.textContent = message + " Użyj przycisku Sprawdź ponownie.";
    for (const kind of remaining) {
      const section = document.getElementById(kind);
      section?.removeAttribute("aria-busy");
      const label = section?.querySelector('[role="status"]');
      if (label) label.textContent = "Nie udało się sprawdzić";
      section?.querySelector(".loading-surface")?.remove();
    }
    const apply = document.getElementById("apply-button");
    if (apply) apply.disabled = true;
    document
      .querySelectorAll('#apply-form input[type="checkbox"]')
      .forEach((input) => {
        input.disabled = true;
      });
  }
  stream.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data.kind === "progress") {
        document.getElementById("review-loading-status").textContent =
          data.message;
      } else if (
        ["source", "applications", "configuration"].includes(data.kind)
      ) {
        document.getElementById(data.kind + "-content").innerHTML = data.html;
        remaining.delete(data.kind);
        const count = document.getElementById(data.kind + "-count");
        if (count) count.textContent = data.count;
        const status = document.getElementById("review-loading-status");
        if (status)
          status.textContent = remaining.size
            ? "Sprawdzanie pozostałych elementów…"
            : "Kończenie sprawdzania…";
        bindReviewControls();
      } else if (data.kind === "complete") {
        if (remaining.size) throw new Error("Incomplete review");
        document.getElementById("overview-content").innerHTML = data.html;
        stream.close();
      } else if (data.kind === "error") {
        failReview(data.message);
      }
    } catch {
      failReview("Nie udało się odebrać pełnego przeglądu.");
    }
  };
  stream.onerror = () =>
    failReview("Połączenie ze sprawdzaniem zostało przerwane.");
  window.addEventListener("pagehide", () => stream.close(), { once: true });
}
window.addEventListener("pageshow", (event) => {
  // Safari may restore disabled controls and an obsolete review from its page cache.
  if (event.persisted) window.location.reload();
});
