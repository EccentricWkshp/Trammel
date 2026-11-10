// Shop-floor page behavior: service worker registration and QR scanning.
// Loaded as a static file — the CSP allows no inline script.
(function () {
  "use strict";

  if ("serviceWorker" in navigator) {
    const sw = document.querySelector('link[rel="manifest"]')?.dataset.sw;
    if (sw) navigator.serviceWorker.register(sw, { scope: sw.replace(/sw\.js$/, "") }).catch(() => {});
  }

  const video = document.getElementById("scan-video");
  const status = document.getElementById("scan-status");
  if (!video) return;

  function say(text) { if (status) status.textContent = text; }

  if (!("BarcodeDetector" in window)) {
    say("This browser cannot scan with the camera. Use a barcode scanner or type the code below.");
    return;
  }
  if (!window.isSecureContext) {
    say("The camera needs HTTPS. Use a barcode scanner or type the code below.");
    return;
  }

  // Every format this browser can read: QR labels, and the UPC, EAN and Code 128
  // barcodes on packaging.
  const formats = (typeof window.BarcodeDetector.getSupportedFormats === "function")
    ? window.BarcodeDetector.getSupportedFormats()
    : Promise.resolve(["qr_code", "code_128", "ean_13", "upc_a"]);
  formats
    .then((list) => Promise.all([
      new window.BarcodeDetector({ formats: list.length ? list : ["qr_code"] }),
      navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false }),
    ]))
    .then(([detector, stream]) => {
      video.srcObject = stream;
      video.play();
      say("Point the camera at a label, traveler or barcode.");
      const tick = async () => {
        try {
          const codes = await detector.detect(video);
          if (codes.length) {
            const value = codes[0].rawValue;
            stream.getTracks().forEach((t) => t.stop());
            say("Opening…");
            window.location.href = video.dataset.find + "?q=" + encodeURIComponent(value);
            return;
          }
        } catch (e) { /* frame not ready */ }
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    })
    .catch(() => say("The camera could not be opened. Use a barcode scanner or type the code below."));
})();
