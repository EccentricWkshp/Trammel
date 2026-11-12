// The shop schedule's timeline (workorders/schedule.html): pointing at a bar,
// or focusing it, shows what it is under the timeline. On a touch screen the
// first tap on a bar shows it and a second tap opens the work order.
(function () {
  "use strict";
  var svg = document.querySelector("svg.schedule");
  var detail = document.getElementById("schedule-detail");
  if (!svg || !detail) return;
  var resting = detail.textContent;
  var picked = null;
  var touch = false;

  function show(link) {
    var title = link.querySelector("title");
    if (!title) return;
    if (picked && picked !== link) picked.classList.remove("picked");
    picked = link;
    link.classList.add("picked");
    detail.textContent = title.textContent;
    detail.classList.add("showing");
  }

  function clear() {
    if (picked) picked.classList.remove("picked");
    picked = null;
    detail.textContent = resting;
    detail.classList.remove("showing");
  }

  function barOf(event) {
    var link = event.target.closest ? event.target.closest("a") : null;
    return link && svg.contains(link) ? link : null;
  }

  svg.addEventListener("pointerdown", function (event) {
    touch = event.pointerType === "touch" || event.pointerType === "pen";
  });
  svg.addEventListener("mouseover", function (event) {
    var link = barOf(event);
    if (link && !touch) show(link);
  });
  svg.addEventListener("mouseleave", function () {
    if (!touch) clear();
  });
  svg.addEventListener("focusin", function (event) {
    var link = barOf(event);
    if (link && !touch) show(link);  // a tap is handled on click
  });
  document.addEventListener("keydown", function () {
    touch = false;
  });
  svg.addEventListener("click", function (event) {
    var link = barOf(event);
    if (!link) {
      clear();
      return;
    }
    if (touch && picked !== link) {
      event.preventDefault();
      show(link);
    }
  });
})();
