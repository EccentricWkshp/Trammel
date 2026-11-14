// The 3D viewer (FR-DOC-4, 0.11.0).
//
// A link to a model's picture marked data-model (the address of its mesh, made
// by the worker: trammel/files/meshes.py) opens here instead of as a picture:
// turned with a drag, moved with a right-drag or Shift-drag, zoomed with the
// wheel or a pinch, with standard views, edges, triangles, a section and the
// model's size, volume and surface area. previews.js calls TrammelViewer.open;
// without WebGL 2 the picture opens as before.
//
// No library: the mesh is a small packed format (see meshes.py), drawn with
// WebGL 2, so the browser never reads the customer's file itself.
(function () {
  "use strict";

  var MAGIC = "TRMESH01";
  var HEADER_BYTES = 40;
  var FOV = (30 * Math.PI) / 180;
  var BASE = [96 / 255, 140 / 255, 182 / 255];

  // ---------------------------------------------------------------------------
  // Matrices: 4×4, column-major, as WebGL takes them.
  // ---------------------------------------------------------------------------

  function multiply(a, b) {
    var out = new Float32Array(16);
    for (var c = 0; c < 4; c++) {
      for (var r = 0; r < 4; r++) {
        var s = 0;
        for (var k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
        out[c * 4 + r] = s;
      }
    }
    return out;
  }

  function perspective(fovy, aspect, near, far) {
    var f = 1 / Math.tan(fovy / 2);
    var out = new Float32Array(16);
    out[0] = f / aspect;
    out[5] = f;
    out[10] = (far + near) / (near - far);
    out[11] = -1;
    out[14] = (2 * far * near) / (near - far);
    return out;
  }

  function orthographic(halfW, halfH, near, far) {
    var out = new Float32Array(16);
    out[0] = 1 / halfW;
    out[5] = 1 / halfH;
    out[10] = -2 / (far - near);
    out[14] = -(far + near) / (far - near);
    out[15] = 1;
    return out;
  }

  function sub(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }
  function add(a, b) { return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]; }
  function scale(a, s) { return [a[0] * s, a[1] * s, a[2] * s]; }
  function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
  function cross(a, b) {
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  }
  function normalize(a) {
    var l = Math.sqrt(dot(a, a)) || 1;
    return [a[0] / l, a[1] / l, a[2] / l];
  }

  function lookAt(eye, target, up) {
    var f = normalize(sub(target, eye));
    var s = normalize(cross(f, up));
    var u = cross(s, f);
    var out = new Float32Array(16);
    out[0] = s[0]; out[4] = s[1]; out[8] = s[2];
    out[1] = u[0]; out[5] = u[1]; out[9] = u[2];
    out[2] = -f[0]; out[6] = -f[1]; out[10] = -f[2];
    out[12] = -dot(s, eye);
    out[13] = -dot(u, eye);
    out[14] = dot(f, eye);
    out[15] = 1;
    return out;
  }

  // ---------------------------------------------------------------------------
  // The mesh
  // ---------------------------------------------------------------------------

  function parse(buffer) {
    if (buffer.byteLength < HEADER_BYTES) throw new Error("short");
    var head = new DataView(buffer, 0, HEADER_BYTES);
    var magic = "";
    for (var i = 0; i < 8; i++) magic += String.fromCharCode(head.getUint8(i));
    if (magic !== MAGIC) throw new Error("not a mesh");
    var t = head.getUint32(8, true);
    var e = head.getUint32(12, true);
    var low = [head.getFloat32(16, true), head.getFloat32(20, true), head.getFloat32(24, true)];
    var size = [head.getFloat32(28, true), head.getFloat32(32, true), head.getFloat32(36, true)];
    var need = HEADER_BYTES + t * 18 + e * 12;
    if (buffer.byteLength < need) throw new Error("short");
    return {
      triangles: t,
      edges: e,
      low: low,
      size: size,
      tri: new Uint16Array(buffer, HEADER_BYTES, t * 9),
      lines: new Uint16Array(buffer, HEADER_BYTES + t * 18, e * 6),
    };
  }

  // ---------------------------------------------------------------------------
  // Numbers and units
  // ---------------------------------------------------------------------------

  function num(value, places) {
    return value.toLocaleString("en-US", { maximumFractionDigits: places });
  }

  // Size, volume and surface area in the shop's units, and the other system's
  // after it. mm, mm² and mm³ in; `factor` is the STL unit chosen (1 or 25.4).
  function facts(info, metric, factor) {
    var size = info.size.map(function (v) { return v * factor; });
    var vol = info.volume * factor * factor * factor;
    var area = info.area * factor * factor;
    var mm = size.map(function (v) { return num(v, 1); }).join(" × ") + " mm";
    var inch = size.map(function (v) { return num(v / 25.4, 3); }).join(" × ") + " in";
    var cm3 = num(vol / 1000, 2) + " cm³";
    var in3 = num(vol / 16387.064, 3) + " in³";
    var cm2 = num(area / 100, 2) + " cm²";
    var in2 = num(area / 645.16, 2) + " in²";
    return metric
      ? { size: [mm, inch], volume: [cm3, in3], area: [cm2, in2] }
      : { size: [inch, mm], volume: [in3, cm3], area: [in2, cm2] };
  }

  // ---------------------------------------------------------------------------
  // WebGL
  // ---------------------------------------------------------------------------

  var MESH_VS =
    "#version 300 es\n" +
    "in vec3 aPos;\n" +
    "uniform vec3 uLow; uniform vec3 uSize; uniform mat4 uViewProj;\n" +
    "out vec3 vWorld; out vec3 vBary;\n" +
    "void main() {\n" +
    "  vec3 p = uLow + aPos * uSize;\n" +
    "  vWorld = p;\n" +
    "  int k = gl_VertexID % 3;\n" +
    "  vBary = vec3(k == 0, k == 1, k == 2);\n" +
    "  gl_Position = uViewProj * vec4(p, 1.0);\n" +
    "}\n";

  var MESH_FS =
    "#version 300 es\n" +
    "precision highp float;\n" +
    "in vec3 vWorld; in vec3 vBary;\n" +
    "uniform vec3 uEye; uniform vec3 uKey; uniform vec3 uFill; uniform vec3 uBase;\n" +
    "uniform vec4 uPlane; uniform int uCut; uniform int uCap; uniform int uInverted; uniform int uWire;\n" +
    "out vec4 color;\n" +
    "void main() {\n" +
    "  if (uCut == 1 && dot(vWorld, uPlane.xyz) > uPlane.w) discard;\n" +
    "  bool inside = uCut == 1 && uCap == 1 && (gl_FrontFacing == (uInverted == 1));\n" +
    "  if (inside) { color = vec4(0.86, 0.42, 0.33, 1.0); return; }\n" +
    "  vec3 n = normalize(cross(dFdx(vWorld), dFdy(vWorld)));\n" +
    "  vec3 v = normalize(uEye - vWorld);\n" +
    "  if (dot(n, v) < 0.0) n = -n;\n" +
    "  float key = max(dot(n, uKey), 0.0);\n" +
    "  float fill = max(dot(n, uFill), 0.0);\n" +
    "  vec3 h = normalize(uKey + v);\n" +
    "  float spec = pow(max(dot(n, h), 0.0), 40.0) * 0.18;\n" +
    "  vec3 lit = uBase * (0.34 + 0.56 * key + 0.18 * fill) + vec3(spec);\n" +
    "  if (uWire == 1) {\n" +
    "    vec3 w = fwidth(vBary);\n" +
    "    vec3 s = smoothstep(vec3(0.0), w * 1.2, vBary);\n" +
    "    lit = mix(vec3(0.16, 0.24, 0.33), lit, min(min(s.x, s.y), s.z));\n" +
    "  }\n" +
    "  color = vec4(lit, 1.0);\n" +
    "}\n";

  var LINE_VS =
    "#version 300 es\n" +
    "in vec3 aPos;\n" +
    "uniform vec3 uLow; uniform vec3 uSize; uniform mat4 uViewProj;\n" +
    "out vec3 vWorld;\n" +
    "void main() {\n" +
    "  vec3 p = uLow + aPos * uSize;\n" +
    "  vWorld = p;\n" +
    "  gl_Position = uViewProj * vec4(p, 1.0);\n" +
    "}\n";

  var LINE_FS =
    "#version 300 es\n" +
    "precision highp float;\n" +
    "in vec3 vWorld;\n" +
    "uniform vec4 uPlane; uniform int uCut; uniform vec3 uColor;\n" +
    "out vec4 color;\n" +
    "void main() {\n" +
    "  if (uCut == 1 && dot(vWorld, uPlane.xyz) > uPlane.w) discard;\n" +
    "  color = vec4(uColor, 1.0);\n" +
    "}\n";

  function compile(gl, vs, fs) {
    function shader(type, source) {
      var s = gl.createShader(type);
      gl.shaderSource(s, source);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    }
    var program = gl.createProgram();
    gl.attachShader(program, shader(gl.VERTEX_SHADER, vs));
    gl.attachShader(program, shader(gl.FRAGMENT_SHADER, fs));
    gl.bindAttribLocation(program, 0, "aPos");
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
    var uniforms = {};
    var count = gl.getProgramParameter(program, gl.ACTIVE_UNIFORMS);
    for (var i = 0; i < count; i++) {
      var name = gl.getActiveUniform(program, i).name;
      uniforms[name] = gl.getUniformLocation(program, name);
    }
    return { program: program, u: uniforms };
  }

  var supported = null;
  function isSupported() {
    if (supported === null) {
      try {
        var probe = document.createElement("canvas");
        supported = !!(window.WebGL2RenderingContext && probe.getContext("webgl2"));
      } catch (e) {
        supported = false;
      }
    }
    return supported;
  }

  // ---------------------------------------------------------------------------
  // The dialog
  // ---------------------------------------------------------------------------

  var ui = null;

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    for (var key in attrs || {}) node.setAttribute(key, attrs[key]);
    if (text) node.textContent = text;
    return node;
  }

  function button(label, title, onClick) {
    var b = el("button", { type: "button", class: "secondary" }, label);
    if (title) b.title = title;
    b.addEventListener("click", onClick);
    return b;
  }

  function check(label, onChange) {
    var wrap = el("label", { class: "mv-check" });
    var box = el("input", { type: "checkbox" });
    box.addEventListener("change", function () { onChange(box.checked); });
    wrap.append(box, document.createTextNode(" " + label));
    return { wrap: wrap, box: box };
  }

  function build() {
    var dialog = el("dialog", { class: "lightbox model-viewer" });
    var close = el("button", { type: "button", class: "lightbox-close", "aria-label": "Close the 3D view" }, "×");
    close.addEventListener("click", function () { dialog.close(); });

    var stage = el("div", { class: "mv-stage" });
    var canvas = el("canvas", {
      tabindex: "0",
      role: "img",
      "aria-label": "3D view. Drag to turn, right-drag or Shift-drag to move, scroll to zoom; arrow keys turn, Shift and arrows move, + and - zoom, 0 fits it.",
    });
    var status = el("p", { class: "mv-status" });
    var axes = el("div", { class: "mv-axes", "aria-hidden": "true" });
    var axisLabels = ["X", "Y", "Z"].map(function (name) {
      var span = el("span", { class: "mv-axis mv-axis-" + name.toLowerCase() }, name);
      axes.appendChild(span);
      return span;
    });
    stage.append(canvas, axes, status);

    var views = el("div", { class: "mv-group", role: "group", "aria-label": "View" });
    views.append(
      button("Fit", "Fit the model in view (0)", function () { viewer.fit(); }),
      button("Front", "Look from the front", function () { viewer.view(-90, 0); }),
      button("Right", "Look from the right", function () { viewer.view(0, 0); }),
      button("Top", "Look from above", function () { viewer.view(-90, 90); }),
      button("3D", "Look from the front, right and above", function () { viewer.view(-54.5, 32.6); })
    );

    var show = el("div", { class: "mv-group", role: "group", "aria-label": "Show" });
    var edges = check("Edges", function (on) { viewer.set("edges", on); });
    var wire = check("Triangles", function (on) { viewer.set("wire", on); });
    var ortho = check("Orthographic", function (on) { viewer.set("ortho", on); });
    show.append(edges.wrap, wire.wrap, ortho.wrap);

    var section = el("div", { class: "mv-group", role: "group", "aria-label": "Section" });
    var axisLabel = el("label", {}, "Section ");
    var axisSelect = el("select");
    [["", "Off"], ["0", "Across X"], ["1", "Across Y"], ["2", "Across Z"]].forEach(function (o) {
      var option = el("option", { value: o[0] }, o[1]);
      axisSelect.appendChild(option);
    });
    axisLabel.appendChild(axisSelect);
    var slider = el("input", { type: "range", min: "0", max: "1000", value: "500", "aria-label": "Where the section cuts", disabled: "" });
    var flip = button("Flip", "Keep the other side", function () { viewer.flipSection(); });
    flip.disabled = true;
    axisSelect.addEventListener("change", function () {
      var on = axisSelect.value !== "";
      slider.disabled = !on;
      flip.disabled = !on;
      viewer.section(on ? Number(axisSelect.value) : null, slider.value / 1000);
    });
    slider.addEventListener("input", function () {
      if (axisSelect.value !== "") viewer.section(Number(axisSelect.value), slider.value / 1000);
    });
    section.append(axisLabel, slider, flip);

    var bar = el("div", { class: "mv-bar" });
    bar.append(views, show, section);

    var factsList = el("dl", { class: "mv-facts" });
    var unitRow = el("p", { class: "mv-unit" });
    var caption = el("p", { class: "lightbox-caption" });
    var title = el("span");
    var picture = el("a", { target: "_blank", rel: "noopener" }, "Open the picture");
    caption.append(title, picture);

    dialog.append(close, stage, bar, factsList, unitRow, caption);
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener("close", function () {
      viewer.load++; // a model still on its way is dropped
      viewer.unload();
    });
    document.body.appendChild(dialog);

    ui = {
      dialog: dialog, canvas: canvas, status: status, axisLabels: axisLabels,
      facts: factsList, unit: unitRow, title: title, picture: picture,
      edges: edges, wire: wire, ortho: ortho, axisSelect: axisSelect, slider: slider, flip: flip,
    };
    var viewer = new Viewer(ui);
    ui.viewer = viewer;
  }

  // ---------------------------------------------------------------------------
  // The viewer
  // ---------------------------------------------------------------------------

  function Viewer(ui) {
    this.ui = ui;
    this.canvas = ui.canvas;
    this.gl = null;
    this.mesh = null;
    this.opts = { edges: false, wire: false, ortho: false };
    this.cut = null; // {axis, at, flip}
    this.pointers = {};
    this.frame = 0;
    this.load = 0; // which load is current, so a late one is dropped
    this.bindInput();
  }

  Viewer.prototype.context = function () {
    if (this.gl) return this.gl;
    var gl = this.canvas.getContext("webgl2", { antialias: true, preserveDrawingBuffer: false });
    if (!gl) throw new Error("no webgl2");
    this.gl = gl;
    this.meshProgram = compile(gl, MESH_VS, MESH_FS);
    this.lineProgram = compile(gl, LINE_VS, LINE_FS);
    var self = this;
    this.canvas.addEventListener("webglcontextlost", function (event) {
      event.preventDefault();
      self.gl = null;
      self.mesh = null;
      self.say("The 3D view stopped. Close it and open it again.");
    });
    if (window.ResizeObserver) {
      new ResizeObserver(function () { self.redraw(); }).observe(this.canvas);
    }
    return gl;
  };

  Viewer.prototype.say = function (text) {
    this.ui.status.textContent = text || "";
    this.ui.status.hidden = !text;
  };

  Viewer.prototype.open = function (url, info, metric) {
    var self = this;
    var ticket = ++this.load;
    this.info = info;
    this.metric = metric;
    this.factor = 1;
    this.unload();
    this.say("Loading the model…");
    this.showFacts();
    var gl;
    try {
      gl = this.context();
    } catch (e) {
      this.say("This browser can't show 3D models. Open the picture instead.");
      return;
    }
    fetch(url, { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.arrayBuffer();
      })
      .then(function (buffer) {
        if (ticket !== self.load) return;
        self.upload(gl, parse(buffer));
        self.say("");
        self.fit();
      })
      .catch(function () {
        if (ticket !== self.load) return;
        self.say("The model couldn't be loaded. Open the picture instead.");
      });
  };

  Viewer.prototype.upload = function (gl, data) {
    var mesh = { data: data, low: data.low, size: data.size };
    mesh.vao = gl.createVertexArray();
    gl.bindVertexArray(mesh.vao);
    mesh.buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, mesh.buffer);
    gl.bufferData(gl.ARRAY_BUFFER, data.tri, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 3, gl.UNSIGNED_SHORT, true, 0, 0);
    mesh.count = data.triangles * 3;
    if (data.edges) {
      mesh.lineVao = gl.createVertexArray();
      gl.bindVertexArray(mesh.lineVao);
      mesh.lineBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, mesh.lineBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, data.lines, gl.STATIC_DRAW);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 3, gl.UNSIGNED_SHORT, true, 0, 0);
      mesh.lineCount = data.edges * 2;
    }
    gl.bindVertexArray(null);
    mesh.center = add(data.low, scale(data.size, 0.5));
    mesh.radius = Math.max(0.5 * Math.sqrt(dot(data.size, data.size)), 1e-6);
    // The section's slider runs across the model, a hair past each side.
    this.mesh = mesh;
    this.ui.edges.box.disabled = !data.edges;
    this.ui.edges.wrap.title = data.edges ? "" : "This model has no sharp edges to show.";
  };

  Viewer.prototype.unload = function () {
    var gl = this.gl;
    var mesh = this.mesh;
    this.mesh = null;
    if (!gl || !mesh) return;
    gl.deleteBuffer(mesh.buffer);
    gl.deleteVertexArray(mesh.vao);
    if (mesh.lineBuffer) {
      gl.deleteBuffer(mesh.lineBuffer);
      gl.deleteVertexArray(mesh.lineVao);
    }
  };

  Viewer.prototype.showFacts = function () {
    var ui = this.ui;
    var info = this.info || {};
    var self = this;
    ui.facts.textContent = "";
    ui.unit.textContent = "";
    if (!info.size) return;
    var f = facts(info, this.metric, this.factor);
    function row(term, values, note) {
      var dt = el("dt", {}, term);
      var dd = el("dd", {}, values[0] + " ");
      dd.appendChild(el("span", { class: "muted" }, "(" + values[1] + ")"));
      if (note) dd.appendChild(el("span", { class: "mv-note" }, " " + note));
      ui.facts.append(dt, dd);
    }
    row("Size", f.size);
    row("Volume", f.volume, info.solid ? "" : "about: the surface isn't closed, so this is approximate");
    row("Surface area", f.area);
    var count = num(info.triangles, 0);
    if (info.shown < info.triangles) count = num(info.shown, 0) + " of " + count + " shown, simplified";
    if (info.truncated) count += "; the file has more than these, which aren't read";
    ui.facts.append(el("dt", {}, "Triangles"), el("dd", {}, count));
    if (info.objects > 1) ui.facts.append(el("dt", {}, "Parts"), el("dd", {}, num(info.objects, 0)));

    if (!info.unit) {
      // STL names no unit: read as millimeters, as slicers do, unless told.
      var label = el("label", {}, "STL files don't say their unit. This one is in ");
      var select = el("select");
      select.append(el("option", { value: "1" }, "millimeters"), el("option", { value: "25.4" }, "inches"));
      select.value = String(this.factor);
      select.addEventListener("change", function () {
        self.factor = Number(select.value);
        self.showFacts();
        ui.unit.querySelector("select").focus();
      });
      label.appendChild(select);
      ui.unit.appendChild(label);
    }
  };

  // -- the camera --------------------------------------------------------------

  Viewer.prototype.view = function (yawDeg, pitchDeg) {
    this.yaw = (yawDeg * Math.PI) / 180;
    this.pitch = (Math.max(-89.9, Math.min(89.9, pitchDeg)) * Math.PI) / 180;
    this.zoom = 1;
    this.pan = [0, 0, 0];
    this.redraw();
  };

  Viewer.prototype.fit = function () {
    if (this.yaw === undefined) return this.view(-54.5, 32.6);
    this.zoom = 1;
    this.pan = [0, 0, 0];
    this.redraw();
  };

  Viewer.prototype.camera = function (aspect) {
    var mesh = this.mesh;
    var dir = [
      Math.cos(this.pitch) * Math.cos(this.yaw),
      Math.cos(this.pitch) * Math.sin(this.yaw),
      Math.sin(this.pitch),
    ];
    var target = add(mesh.center, this.pan);
    var fit = (mesh.radius * 1.08) / Math.sin(FOV / 2) / Math.min(1, aspect);
    var distance = fit / this.zoom;
    var eye = add(target, scale(dir, Math.max(distance, mesh.radius * 0.05)));
    var forward = scale(dir, -1);
    var right = normalize(cross(forward, [0, 0, 1]));
    var up = cross(right, forward);
    var view = lookAt(eye, target, [0, 0, 1]);
    var proj;
    var halfH;
    if (this.opts.ortho) {
      halfH = (mesh.radius * 1.08) / Math.min(1, aspect) / this.zoom;
      eye = add(target, scale(dir, mesh.radius * 4));
      view = lookAt(eye, target, [0, 0, 1]);
      proj = orthographic(halfH * aspect, halfH, mesh.radius * 0.01, mesh.radius * 8);
    } else {
      var near = Math.max(distance - mesh.radius * 1.5, distance * 0.01);
      proj = perspective(FOV, aspect, near, distance + mesh.radius * 2);
      halfH = distance * Math.tan(FOV / 2);
    }
    return {
      eye: eye, forward: forward, right: right, up: up,
      viewProj: multiply(proj, view),
      rotation: view,
      // World units across one CSS pixel at the target, for moving it.
      perPixel: (2 * halfH) / Math.max(this.canvas.clientHeight, 1),
    };
  };

  // -- the section --------------------------------------------------------------

  Viewer.prototype.section = function (axis, at) {
    this.cut = axis === null ? null : { axis: axis, at: at, flip: this.cut ? this.cut.flip : false };
    this.redraw();
  };

  Viewer.prototype.flipSection = function () {
    if (!this.cut) return;
    this.cut.flip = !this.cut.flip;
    this.redraw();
  };

  Viewer.prototype.set = function (name, on) {
    this.opts[name] = on;
    this.redraw();
  };

  // -- drawing ------------------------------------------------------------------

  Viewer.prototype.redraw = function () {
    if (this.frame) return;
    var self = this;
    this.frame = requestAnimationFrame(function () {
      self.frame = 0;
      self.draw();
    });
  };

  Viewer.prototype.draw = function () {
    var gl = this.gl;
    var mesh = this.mesh;
    if (!gl || !mesh || this.yaw === undefined) return;
    var canvas = this.canvas;
    var ratio = Math.min(window.devicePixelRatio || 1, 2);
    var w = Math.max(1, Math.round(canvas.clientWidth * ratio));
    var h = Math.max(1, Math.round(canvas.clientHeight * ratio));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    gl.viewport(0, 0, w, h);
    gl.clearColor(0.957, 0.965, 0.973, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);

    var cam = this.camera(w / h);
    // Lights ride with the camera: a key from the upper left, a fill from the right.
    var key = normalize(add(add(scale(cam.right, -0.5), scale(cam.up, 0.7)), scale(cam.forward, -0.6)));
    var fill = normalize(add(add(scale(cam.right, 0.8), scale(cam.up, -0.2)), scale(cam.forward, -0.4)));

    var plane = [0, 0, 0, 0];
    var cut = this.cut;
    if (cut) {
      var at = mesh.low[cut.axis] + mesh.size[cut.axis] * (cut.at * 1.002 - 0.001);
      var sign = cut.flip ? -1 : 1;
      plane[cut.axis] = sign;
      plane[3] = sign * at;
    }

    var p = this.meshProgram;
    gl.useProgram(p.program);
    gl.uniform3fv(p.u.uLow, mesh.low);
    gl.uniform3fv(p.u.uSize, mesh.size);
    gl.uniformMatrix4fv(p.u.uViewProj, false, cam.viewProj);
    gl.uniform3fv(p.u.uEye, cam.eye);
    gl.uniform3fv(p.u.uKey, key);
    gl.uniform3fv(p.u.uFill, fill);
    gl.uniform3fv(p.u.uBase, BASE);
    gl.uniform4fv(p.u.uPlane, plane);
    gl.uniform1i(p.u.uCut, cut ? 1 : 0);
    gl.uniform1i(p.u.uCap, this.info && this.info.solid ? 1 : 0);
    gl.uniform1i(p.u.uInverted, this.info && this.info.inverted ? 1 : 0);
    gl.uniform1i(p.u.uWire, this.opts.wire ? 1 : 0);
    gl.enable(gl.POLYGON_OFFSET_FILL);
    gl.polygonOffset(1, 1);
    gl.bindVertexArray(mesh.vao);
    gl.drawArrays(gl.TRIANGLES, 0, mesh.count);
    gl.disable(gl.POLYGON_OFFSET_FILL);

    if (this.opts.edges && mesh.lineVao) {
      var l = this.lineProgram;
      gl.useProgram(l.program);
      gl.uniform3fv(l.u.uLow, mesh.low);
      gl.uniform3fv(l.u.uSize, mesh.size);
      gl.uniformMatrix4fv(l.u.uViewProj, false, cam.viewProj);
      gl.uniform4fv(l.u.uPlane, plane);
      gl.uniform1i(l.u.uCut, cut ? 1 : 0);
      gl.uniform3fv(l.u.uColor, [0.1, 0.15, 0.2]);
      gl.bindVertexArray(mesh.lineVao);
      gl.drawArrays(gl.LINES, 0, mesh.lineCount);
    }
    gl.bindVertexArray(null);
    this.cam = cam;
    this.placeAxes(cam);
  };

  // The X, Y and Z labels in the corner, turned with the model.
  Viewer.prototype.placeAxes = function (cam) {
    var axes = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
    var labels = this.ui.axisLabels;
    for (var i = 0; i < 3; i++) {
      var x = dot(axes[i], cam.right);
      var y = dot(axes[i], cam.up);
      var depth = dot(axes[i], cam.forward);
      var style = labels[i].style;
      style.setProperty("--x", (x * 30).toFixed(1) + "px");
      style.setProperty("--y", (-y * 30).toFixed(1) + "px");
      style.setProperty("--len", (Math.sqrt(x * x + y * y) * 30).toFixed(1) + "px");
      style.setProperty("--angle", Math.atan2(-y, x).toFixed(3) + "rad");
      style.opacity = depth > 0.6 ? "0.35" : "1";
    }
  };

  // -- input --------------------------------------------------------------------

  Viewer.prototype.orbit = function (dx, dy) {
    this.yaw -= dx * 0.008;
    this.pitch = Math.max(-1.5691, Math.min(1.5691, this.pitch + dy * 0.008));
    this.redraw();
  };

  Viewer.prototype.move = function (dx, dy) {
    var cam = this.cam;
    if (!cam) return;
    var step = cam.perPixel;
    this.pan = add(this.pan, add(scale(cam.right, -dx * step), scale(cam.up, dy * step)));
    this.redraw();
  };

  Viewer.prototype.zoomBy = function (factor) {
    this.zoom = Math.max(0.05, Math.min(200, this.zoom * factor));
    this.redraw();
  };

  Viewer.prototype.bindInput = function () {
    var self = this;
    var canvas = this.canvas;
    var pointers = this.pointers;
    var pinch = null;

    function spread() {
      var list = Object.keys(pointers).map(function (k) { return pointers[k]; });
      var a = list[0];
      var b = list[1];
      return {
        distance: Math.hypot(a.x - b.x, a.y - b.y),
        x: (a.x + b.x) / 2,
        y: (a.y + b.y) / 2,
      };
    }

    canvas.addEventListener("pointerdown", function (event) {
      if (!self.mesh) return;
      canvas.setPointerCapture(event.pointerId);
      pointers[event.pointerId] = {
        x: event.clientX,
        y: event.clientY,
        move: event.button === 2 || event.button === 1 || event.shiftKey,
      };
      if (Object.keys(pointers).length === 2) pinch = spread();
      event.preventDefault();
    });
    canvas.addEventListener("pointermove", function (event) {
      var p = pointers[event.pointerId];
      if (!p) return;
      var dx = event.clientX - p.x;
      var dy = event.clientY - p.y;
      p.x = event.clientX;
      p.y = event.clientY;
      var count = Object.keys(pointers).length;
      if (count === 2 && pinch) {
        var now = spread();
        if (pinch.distance > 0 && now.distance > 0) self.zoomBy(now.distance / pinch.distance);
        self.move(now.x - pinch.x, now.y - pinch.y);
        pinch = now;
      } else if (count === 1) {
        if (p.move) self.move(dx, dy);
        else self.orbit(dx, dy);
      }
    });
    function lift(event) {
      delete pointers[event.pointerId];
      pinch = Object.keys(pointers).length === 2 ? spread() : null;
    }
    canvas.addEventListener("pointerup", lift);
    canvas.addEventListener("pointercancel", lift);
    canvas.addEventListener("contextmenu", function (event) { event.preventDefault(); });
    canvas.addEventListener("dblclick", function () { self.fit(); });
    canvas.addEventListener("wheel", function (event) {
      if (!self.mesh) return;
      event.preventDefault();
      var lines = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1;
      self.zoomBy(Math.exp(-event.deltaY * lines * 0.0015));
    }, { passive: false });
    canvas.addEventListener("keydown", function (event) {
      if (!self.mesh) return;
      var step = 12;
      var handled = true;
      switch (event.key) {
        case "ArrowLeft": event.shiftKey ? self.move(-step * 2, 0) : self.orbit(-step, 0); break;
        case "ArrowRight": event.shiftKey ? self.move(step * 2, 0) : self.orbit(step, 0); break;
        case "ArrowUp": event.shiftKey ? self.move(0, -step * 2) : self.orbit(0, -step); break;
        case "ArrowDown": event.shiftKey ? self.move(0, step * 2) : self.orbit(0, step); break;
        case "+": case "=": self.zoomBy(1.15); break;
        case "-": case "_": self.zoomBy(1 / 1.15); break;
        case "0": case "Home": self.fit(); break;
        default: handled = false;
      }
      if (handled) event.preventDefault();
    });
  };

  // ---------------------------------------------------------------------------

  function open(link) {
    if (!isSupported()) return false;
    var info;
    try {
      info = JSON.parse(link.getAttribute("data-model-info") || "null");
    } catch (e) {
      info = null;
    }
    if (!info) return false;
    if (!ui) build();
    var title = link.getAttribute("data-lightbox") || "";
    ui.dialog.setAttribute("aria-label", title ? "3D view of " + title : "3D view");
    ui.title.textContent = title;
    ui.picture.href = link.href;
    ui.edges.box.checked = false;
    ui.wire.box.checked = false;
    ui.ortho.box.checked = false;
    ui.axisSelect.value = "";
    ui.slider.value = "500";
    ui.slider.disabled = true;
    ui.flip.disabled = true;
    var viewer = ui.viewer;
    viewer.opts = { edges: false, wire: false, ortho: false };
    viewer.cut = null;
    viewer.yaw = undefined;
    ui.dialog.showModal();
    viewer.open(link.getAttribute("data-model"), info, link.getAttribute("data-units") === "metric");
    ui.canvas.focus();
    return true;
  }

  window.TrammelViewer = { open: open, supported: isSupported, parse: parse, facts: facts };
})();
