// A quote's options and alternatives (FR-QUO-11): picking one submits the
// choices form, so the page shows the total for what is picked. Without
// script, the form's own button does the same.
(function () {
  "use strict";
  document.querySelectorAll("form[data-autosubmit]").forEach(function (form) {
    form.addEventListener("change", function () {
      if (form.requestSubmit) form.requestSubmit(); else form.submit();
    });
    var button = form.querySelector("[data-autosubmit-button]");
    if (button) button.hidden = true;
  });
})();
