// The campaign form and email editor under /manage/. CSP allows only 'self' scripts, so this lives here.
//
// - `[data-show-for-audience="a b"]` / `[data-show-for-template="k"]` blocks show only while the
//   audience / email select holds one of those values.
// - `[data-filter-list]` text inputs hide the `[data-filter-item]` rows of the list they name whose
//   text does not contain what was typed; `[data-check-visible]` buttons tick every visible row.
(() => {
  const toggle = () => {
    const audience = document.querySelector("[name=audience]")?.value;
    const template = document.querySelector("[name=template_key]")?.value;
    document.querySelectorAll("[data-show-for-audience]").forEach((block) => {
      block.hidden = !block.dataset.showForAudience.split(" ").includes(audience);
    });
    document.querySelectorAll("[data-show-for-template]").forEach((block) => {
      block.hidden = !block.dataset.showForTemplate.split(" ").includes(template);
    });
  };

  const filter = (input) => {
    const list = document.getElementById(input.dataset.filterList);
    if (!list) return;
    const needle = input.value.trim().toLowerCase();
    list.querySelectorAll("[data-filter-item]").forEach((item) => {
      item.hidden = needle !== "" && !item.textContent.toLowerCase().includes(needle);
    });
  };

  document.addEventListener("change", (event) => {
    if (event.target.matches("[name=audience], [name=template_key]")) toggle();
  });
  document.addEventListener("input", (event) => {
    if (event.target.matches("[data-filter-list]")) filter(event.target);
  });
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-check-visible]");
    if (!button) return;
    const list = document.getElementById(button.dataset.checkVisible);
    const check = button.dataset.value !== "0";
    list?.querySelectorAll("[data-filter-item]:not([hidden]) input[type=checkbox]").forEach((box) => {
      box.checked = check;
    });
  });
  document.addEventListener("DOMContentLoaded", toggle);
})();
