/* Signature pads (FR-AGR-7). A box marked data-signature holds a canvas, a
   Clear button and names the hidden input the drawing goes into, as a PNG. */
(function () {
  "use strict";

  function setup(box) {
    var canvas = box.querySelector("canvas");
    var input = document.getElementById(box.getAttribute("data-signature"));
    var clear = box.querySelector("[data-signature-clear]");
    if (!canvas || !input || !canvas.getContext) return;
    var ctx = canvas.getContext("2d");
    var drawing = false;
    var inked = false;
    var last = null;

    function fit() {
      var rect = canvas.getBoundingClientRect();
      var ratio = window.devicePixelRatio || 1;
      canvas.width = Math.round(rect.width * ratio);
      canvas.height = Math.round(rect.height * ratio);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.lineWidth = 2.4;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.strokeStyle = "#111";
    }

    function at(event) {
      var rect = canvas.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    function stop() {
      if (!drawing) return;
      drawing = false;
      if (inked) input.value = canvas.toDataURL("image/png");
    }

    canvas.addEventListener("pointerdown", function (event) {
      drawing = true;
      last = at(event);
      if (canvas.setPointerCapture) canvas.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    canvas.addEventListener("pointermove", function (event) {
      if (!drawing) return;
      var point = at(event);
      ctx.beginPath();
      ctx.moveTo(last.x, last.y);
      ctx.lineTo(point.x, point.y);
      ctx.stroke();
      last = point;
      inked = true;
      event.preventDefault();
    });
    canvas.addEventListener("pointerup", stop);
    canvas.addEventListener("pointercancel", stop);
    if (clear) {
      clear.addEventListener("click", function () {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        inked = false;
        input.value = "";
      });
    }
    // Turning a tablet resizes the box; redraw the empty pad, never a signature.
    window.addEventListener("resize", function () {
      if (!inked) fit();
    });
    box.classList.add("signature-ready");
    fit();
  }

  function start() {
    var boxes = document.querySelectorAll("[data-signature]");
    for (var i = 0; i < boxes.length; i++) setup(boxes[i]);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
