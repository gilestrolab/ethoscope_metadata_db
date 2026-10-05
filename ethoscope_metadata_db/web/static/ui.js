// "More columns" toggles, remembered per table in this browser (a convenience only).
(function () {
  function key(id) { return "census-cols:" + id; }
  function apply(button, on) {
    var table = document.getElementById(button.dataset.toggleCols);
    if (!table) return;
    table.classList.toggle("show-all", on);
    button.textContent = on ? "Fewer columns" : "More columns";
    button.setAttribute("aria-pressed", on ? "true" : "false");
  }
  function restore(root) {
    root.querySelectorAll("[data-toggle-cols]").forEach(function (button) {
      var on = false;
      try { on = localStorage.getItem(key(button.dataset.toggleCols)) === "1"; } catch (e) {}
      apply(button, on);
    });
  }
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-toggle-cols]");
    if (!button) return;
    var table = document.getElementById(button.dataset.toggleCols);
    var on = !(table && table.classList.contains("show-all"));
    apply(button, on);
    try { localStorage.setItem(key(button.dataset.toggleCols), on ? "1" : ""); } catch (e) {}
  });
  document.addEventListener("DOMContentLoaded", function () { restore(document); });
  document.addEventListener("htmx:afterSwap", function (event) { restore(event.target); });
})();
