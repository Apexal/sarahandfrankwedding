// The staff dashboard's copy-link buttons: `<button data-copy="https://…">` puts the value on the
// clipboard and says so for a moment. CSP allows only 'self' scripts, so this lives here, not inline.
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const label = button.dataset.label || button.textContent;
  button.dataset.label = label;
  try {
    await navigator.clipboard.writeText(button.dataset.copy);
    button.textContent = "Copied";
  } catch {
    button.textContent = "Copy failed";
  }
  setTimeout(() => {
    button.textContent = label;
  }, 1500);
});
