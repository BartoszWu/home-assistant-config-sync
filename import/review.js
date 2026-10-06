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
  if (!form) return;
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

const form = document.getElementById("apply-form");
if (form) {
  const selectable = [...form.querySelectorAll("input[data-bulk-selectable]")];
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
if (sourceSelect) {
  sourceSelect.addEventListener("change", (event) => {
    const sourceForm = event.target.form;
    const sha = sourceForm.querySelector('input[name="source_sha"]');
    if (sha) sha.value = "";
    if (!markPageBusy("Sprawdzanie źródła…")) return;
    submitAfterPaint(sourceForm);
  });
}
