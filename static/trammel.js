/* Trammel — small progressive enhancements. Every page works without this file. */
(function () {
  "use strict";

  // Multi-select dropdowns: keep the closed box's summary in step with the ticks,
  // and close an open dropdown on a click outside it or on Escape.
  function summarize(box) {
    var chosen = Array.prototype.filter
      .call(box.querySelectorAll("input[type=checkbox]"), function (i) { return i.checked; })
      .map(function (i) { return i.parentElement.textContent.trim(); });
    box.querySelector(".multiselect-chosen").textContent =
      chosen.length ? chosen.join(", ") : box.dataset.placeholder;
  }
  document.querySelectorAll("details.multiselect").forEach(function (box) {
    box.addEventListener("change", function () { summarize(box); });
  });
  document.addEventListener("click", function (event) {
    document.querySelectorAll("details.multiselect[open]").forEach(function (box) {
      if (!box.contains(event.target)) box.open = false;
    });
  });
  // Settings that only matter while a switch is on ([data-shown-by="<checkbox id>"]).
  // The server already hides them when the saved switch is off; this follows the
  // box as it is ticked, before saving.
  document.querySelectorAll("[data-shown-by]").forEach(function (group) {
    var box = document.getElementById(group.dataset.shownBy);
    if (!box) return;
    function follow() { group.hidden = !box.checked; }
    box.addEventListener("change", follow);
    follow();
  });
  // Script-driven tabs ([data-tabs]): without script every panel shows, one after another.
  document.querySelectorAll("[data-tabs]").forEach(function (bar) {
    var tabs = Array.prototype.slice.call(bar.querySelectorAll("[role=tab]"));
    var panels = tabs.map(function (t) { return document.getElementById(t.getAttribute("aria-controls")); });
    var form = document.getElementById("template-form");
    var previewButton = form && form.querySelector("[data-preview-submit]");
    function select(index, focus) {
      tabs.forEach(function (t, i) {
        var on = i === index;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        panels[i].hidden = !on;
      });
      if (focus) tabs[index].focus();
      if (tabs[index].hasAttribute("data-preview-tab") && previewButton) form.requestSubmit(previewButton);
    }
    tabs.forEach(function (t, i) {
      t.addEventListener("click", function () { select(i, false); });
      t.addEventListener("keydown", function (event) {
        var step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
        if (step) { event.preventDefault(); select((i + step + tabs.length) % tabs.length, true); }
      });
    });
    if (form && previewButton) {
      form.addEventListener("submit", function (event) {
        if (event.submitter === previewButton) {
          var i = tabs.findIndex(function (t) { return t.hasAttribute("data-preview-tab"); });
          tabs.forEach(function (t, n) { t.setAttribute("aria-selected", n === i ? "true" : "false"); panels[n].hidden = n !== i; });
        }
      });
    }
    document.querySelectorAll(".js-hide").forEach(function (el) { el.hidden = true; });
    bar.hidden = false;
    select(0, false);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    document.querySelectorAll("details.multiselect[open]").forEach(function (box) {
      box.open = false;
      box.querySelector("summary").focus();
    });
  });
})();
