// File previews (FR-DOC-4).
//
// Lightbox: a link marked data-lightbox (its value is the caption) shows its
// picture over the page, closed with Escape, the close button or a click
// outside the picture. Ctrl-, Cmd- or Shift-click, or no script at all, opens
// the link in a new tab as usual.
//
// Waiting previews: a placeholder marked data-preview-wait (the preview's
// status address) asks every few seconds whether the preview is ready, and
// swaps itself for the picture when it is; it stops after a few minutes, or
// when there turns out to be no preview to wait for.
(function () {
  "use strict";
  var dialog, image, caption, newTab;

  function build() {
    dialog = document.createElement("dialog");
    dialog.className = "lightbox";
    var close = document.createElement("button");
    close.type = "button";
    close.className = "lightbox-close";
    close.setAttribute("aria-label", "Close the preview");
    close.textContent = "×";
    close.addEventListener("click", function () { dialog.close(); });
    image = document.createElement("img");
    var bar = document.createElement("p");
    bar.className = "lightbox-caption";
    caption = document.createElement("span");
    newTab = document.createElement("a");
    newTab.target = "_blank";
    newTab.rel = "noopener";
    newTab.textContent = "Open in a new tab";
    bar.append(caption, newTab);
    dialog.append(close, image, bar);
    // A click on the backdrop lands on the dialog itself.
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener("close", function () { image.removeAttribute("src"); });
    document.body.appendChild(dialog);
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("a[data-lightbox]");
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (!window.HTMLDialogElement) return;
    event.preventDefault();
    if (!dialog) build();
    var title = link.getAttribute("data-lightbox") || "";
    image.alt = title ? "Preview of " + title : "Preview";
    image.src = link.href;
    caption.textContent = title;
    newTab.href = link.href;
    dialog.setAttribute("aria-label", image.alt);
    dialog.showModal();
  });

  var GIVE_UP_MS = 4 * 60 * 1000;

  function showPreview(slot) {
    var link = document.createElement("a");
    link.href = slot.getAttribute("data-preview-href");
    link.target = "_blank";
    link.rel = "noopener";
    if (slot.hasAttribute("data-preview-lightbox")) {
      link.setAttribute("data-lightbox", slot.getAttribute("data-preview-title") || "");
    }
    var picture = document.createElement("img");
    picture.alt = slot.getAttribute("data-preview-alt") || "";
    picture.src = slot.getAttribute("data-preview-src");
    link.appendChild(picture);
    slot.replaceWith(link);
  }

  function noPreview(slot) {
    var note = slot.querySelector("small");
    if (note) note.remove();
    slot.classList.remove("preview-wait");
  }

  function wait(slot) {
    var started = Date.now();
    var delay = 2000;
    function ask() {
      fetch(slot.getAttribute("data-preview-wait"), {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (state) {
          if (state && state.ready) return showPreview(slot);
          if (state && !state.waiting) return noPreview(slot);
          next();
        })
        .catch(next);
    }
    function next() {
      if (Date.now() - started > GIVE_UP_MS) return noPreview(slot);
      delay = Math.min(delay * 1.5, 15000);
      setTimeout(ask, delay);
    }
    setTimeout(ask, delay);
  }

  function start() {
    document.querySelectorAll("[data-preview-wait]").forEach(wait);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
