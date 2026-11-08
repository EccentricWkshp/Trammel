// Live preview of the machine rate calculator. The server (trammel/shop/rates.py)
// does the same arithmetic when the form is saved; this only shows it sooner.
(function () {
  "use strict";
  var form = document.querySelector(".machine-form");
  if (!form) return;
  var script = document.currentScript || document.querySelector("script[data-electricity]");
  var electricity = parseFloat(script && script.dataset.electricity) || 0;
  var presetsNode = document.getElementById("machine-presets");
  var presets = presetsNode ? JSON.parse(presetsNode.textContent) : {};
  var money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
  var plain = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

  function field(name) { return form.querySelector('[data-rate="' + name + '"]'); }
  function num(name) {
    var el = field(name);
    var v = el ? parseFloat(el.value) : NaN;
    return isNaN(v) ? 0 : v;
  }
  function wear(i, key) { return form.querySelector('[data-wear="' + i + "-" + key + '"]'); }
  function wearRows() {
    var rows = [];
    for (var i = 0; ; i++) {
      var name = wear(i, "name");
      if (!name) break;
      rows.push({ name: name, cost: wear(i, "cost"), life: wear(i, "life") });
    }
    return rows;
  }

  function cell(tag, text, cls) {
    var el = document.createElement(tag);
    el.textContent = text;
    if (cls) el.className = cls;
    return el;
  }

  function calculate() {
    var rows = [], notes = [];
    var purchase = num("purchase_cost"), salvage = num("salvage_value"), life = num("lifespan_hours");
    if (purchase && life) {
      rows.push(["Ownership", (salvage ? "(" + plain.format(purchase) + " − " + plain.format(salvage) + " resale)" : plain.format(purchase)) + " ÷ " + plain.format(life) + " h", Math.max(0, purchase - salvage) / life]);
    } else if (purchase) {
      notes.push("Enter the expected life in hours to spread the purchase price.");
    }
    var service = num("maintenance_cost"), interval = num("maintenance_interval_hours");
    if (service && interval) rows.push(["Maintenance", plain.format(service) + " every " + plain.format(interval) + " h", service / interval]);
    else if (service) notes.push("Enter how many hours pass between services.");
    wearRows().forEach(function (r) {
      var c = parseFloat(r.cost.value), h = parseFloat(r.life.value);
      if (c && h) rows.push([r.name.value || "Wear part", plain.format(c) + " every " + plain.format(h) + " h", c / h]);
    });
    var kw = num("power_kw");
    if (kw) {
      if (electricity) rows.push(["Power", kw + " kW × " + electricity + "/kWh", kw * electricity]);
      else notes.push("Set the electricity price in Shop rates to include power.");
    }
    var consumables = num("consumables_per_hour");
    if (consumables) rows.push(["Consumables", "per hour", consumables]);
    var fixed = num("annual_fixed_costs"), yearly = num("annual_hours");
    if (fixed && yearly) rows.push(["Fixed costs", plain.format(fixed) + " a year ÷ " + plain.format(yearly) + " h", fixed / yearly]);
    else if (fixed) notes.push("Enter the hours run per year to spread the yearly fixed costs.");

    var running = rows.reduce(function (s, r) { return s + r[2]; }, 0);
    var failurePct = num("failure_pct"), markupPct = num("markup_pct");
    var failure = running * failurePct / 100;
    var cost = Math.round((running + failure) * 100) / 100;
    var suggested = Math.round(cost * (1 + markupPct / 100) * 100) / 100;
    return { rows: rows, notes: notes, failurePct: failurePct, failure: failure, cost: cost, markupPct: markupPct, suggested: suggested };
  }

  function render() {
    var b = calculate();
    var body = form.querySelector("[data-rate-rows]");
    body.replaceChildren();
    if (!b.rows.length) {
      var empty = document.createElement("tr");
      empty.className = "muted";
      var td = cell("td", "Fill in the calculator to see what an hour of this machine costs.");
      td.colSpan = 2;
      empty.appendChild(td);
      body.appendChild(empty);
    }
    b.rows.forEach(function (r) {
      var tr = document.createElement("tr");
      var th = cell("th", r[0]);
      th.scope = "row";
      th.appendChild(cell("small", r[1]));
      tr.appendChild(th);
      tr.appendChild(cell("td", money.format(r[2]), "num"));
      body.appendChild(tr);
    });
    form.querySelector("[data-rate-failure]").hidden = !b.failure;
    form.querySelector("[data-rate-failure-pct]").textContent = b.failurePct + "%";
    form.querySelector("[data-rate-failure-amount]").textContent = money.format(b.failure);
    form.querySelector("[data-rate-cost]").textContent = money.format(b.cost);
    form.querySelector("[data-rate-suggested]").textContent = money.format(b.suggested);
    form.querySelector("[data-rate-markup]").textContent = b.markupPct ? "with " + b.markupPct + "% markup" : "no markup set";
    var list = form.querySelector("[data-rate-notes]");
    list.replaceChildren.apply(list, b.notes.map(function (n) { return cell("li", n); }));
  }

  function applyPreset(fill) {
    var preset = presets[field("kind").value];
    if (!preset) return;
    var rows = wearRows();
    var empty = rows.every(function (r) { return !r.name.value && !r.cost.value && !r.life.value; });
    if (fill && empty) {
      preset.wear_parts.forEach(function (p, i) {
        if (!rows[i]) return;
        rows[i].name.value = p[0];
        rows[i].cost.value = p[1];
        rows[i].life.value = p[2];
      });
    }
    // Typical values appear as placeholders, never as entries.
    form.querySelectorAll("[data-rate]").forEach(function (el) {
      if (el.tagName === "INPUT") el.placeholder = (preset.hints || {})[el.dataset.rate] ? "e.g. " + preset.hints[el.dataset.rate] : "";
    });
    render();
  }

  form.addEventListener("input", render);
  field("kind").addEventListener("change", function () { applyPreset(true); });
  applyPreset(false);
})();
