/**
 * Beaudesert fan reversal and supervision script for Shelly Pro 2PM
 * Complete replacement for fan_reverse.js
 *
 * Wiring (matches the wiring diagram):
 *   Switch 0 (O1) = master on off, feeds the transformer L in.
 *                   O1 power meters transformer plus all three fans.
 *   Switch 1 (O2) = Finder 48.31 direction coil.
 *                   O2 open = LF forward (down air), O2 closed = LR reverse (up air).
 *                   O2 power meters the coil only, a watt or two.
 *
 * Functions:
 *   1. Safe reversal sequence: O1 off, verified, coast dwell, O2 flipped and
 *      verified, coil wattage confirmed when reverse selected, settle, O1 on.
 *   2. Stall protection: after any O1 close, once the grace period has passed,
 *      sustained power above STALL_W opens O1 and latches a fault.
 *   3. Low tap protection: sustained power below MIN_RUN_W while O1 is closed
 *      means the dial is too low or a fan is disconnected; O1 is opened.
 *   4. Lockout: reversal commands are ignored while a sequence runs or while
 *      a fault is latched. Clear a fault by turning O1 on from the app or HA,
 *      which rearms supervision.
 *   5. Fault publishing: the latched fault is mirrored to a virtual boolean
 *      (Boolean:FAULT_BOOL_ID, "Fan fault") so Home Assistant can read it as an
 *      entity instead of inferring it. Map that entity in the integration's
 *      "Ceiling fans & cooling" step (the Fan fault field).
 *
 * Triggers:
 *   Virtual button 200 "Reverse fans" (created on first run, visible in app).
 *   Home Assistant or local RPC:
 *     http://<ip>/rpc/Button.Trigger?id=200&event="single_push"
 *   Use device authentication and keep the device on the management VLAN.
 *
 * COMMISSIONING REQUIRED: the three *_W thresholds below are placeholders.
 * Measure real values first (procedure in the accompanying notes) and set:
 *   MIN_RUN_W  a little below the lowest acceptable running tap power
 *   STALL_W    between the highest normal running power and measured stall power
 *   COIL_MIN_W a little below the measured coil draw
 */

// ---------- configuration ----------
var MASTER_ID   = 0;
var DIR_ID      = 1;
// Coast-down time: O1 stays OFF this long so the blades stop BEFORE O2 flips
// and O1 re-energises. It must exceed the real full-stop time — measured at
// ~5 min on these fans. Too short and the coil flips into a still-spinning fan
// (reverse torque, stall-trip, failed reversal). Err long; a slower reversal
// is harmless. Time the blades to a dead stop from full speed and set above it.
var DWELL_MS    = 300000;  // coast down time (~5 min full stop; verify + margin)
var SETTLE_MS   = 1500;    // relay settle before re energising
var VERIFY_MS   = 800;     // wait before verifying a switch command
var BUTTON_ID   = 200;     // virtual button "Reverse fans"
var FAULT_BOOL_ID = 201;   // virtual boolean "Fan fault" (read in Home Assistant)

// supervision thresholds, WATTS. SET FROM COMMISSIONING MEASUREMENTS.
var MIN_RUN_W    = 25;     // below this while running = tap too low or fans missing
var STALL_W      = 260;    // above this while running = stall or fault
var COIL_MIN_W   = 0.5;    // O2 must draw at least this when closed
var GRACE_MS     = 20000;  // ignore power for this long after O1 closes (inrush, spin up)
var TRIP_COUNT   = 3;      // consecutive bad polls required to trip
var POLL_MS      = 5000;   // supervision poll interval

// ---------- state ----------
var busy = false;
var fault = false;
var graceUntil = 0;
var badPolls = 0;

function log(m) { print("[fan] " + m); }

// Mirror the latched fault to the virtual boolean so Home Assistant can read it.
function publishFault(v) {
  Shelly.call("Boolean.Set", { id: FAULT_BOOL_ID, value: v });
}

function abortSafe(reason) {
  log("ABORT: " + reason + " Forcing both outputs off.");
  Shelly.call("Switch.Set", { id: MASTER_ID, on: false });
  Shelly.call("Switch.Set", { id: DIR_ID, on: false });
  busy = false;
}

function tripFault(reason) {
  fault = true;
  publishFault(true);
  log("FAULT LATCHED: " + reason + " O1 opened. Investigate, then switch O1 on to rearm.");
  Shelly.call("Switch.Set", { id: MASTER_ID, on: false });
}

function setAndVerify(id, on, okCb) {
  Shelly.call("Switch.Set", { id: id, on: on }, function (r, err, msg) {
    if (err !== 0) { abortSafe("Switch.Set id " + id + " failed: " + msg); return; }
    Timer.set(VERIFY_MS, false, function () {
      Shelly.call("Switch.GetStatus", { id: id }, function (st, e2) {
        if (e2 !== 0 || st.output !== on) {
          abortSafe("verify failed on switch " + id + " expected " + on); return;
        }
        okCb();
      });
    });
  });
}

// confirm the Finder coil is really drawing current when O2 is closed
function verifyCoil(okCb) {
  Timer.set(1200, false, function () {
    Shelly.call("Switch.GetStatus", { id: DIR_ID }, function (st, err) {
      if (err !== 0) { abortSafe("coil status read failed"); return; }
      if (typeof st.apower === "number" && st.apower < COIL_MIN_W) {
        abortSafe("O2 closed but coil draws " + st.apower +
                  " W. Coil circuit open or relay failed. Direction NOT trusted.");
        return;
      }
      okCb();
    });
  });
}

function doReverse() {
  if (busy)  { log("Sequence already running, ignored."); return; }
  if (fault) { log("Fault latched, reversal refused. Rearm first."); return; }
  busy = true;

  Shelly.call("Switch.GetStatus", { id: DIR_ID }, function (dirSt, err) {
    if (err !== 0) { abortSafe("could not read direction relay"); return; }
    var newDir = !dirSt.output;
    log("Reversal to O2=" + newDir + (newDir ? " (reverse, up air)" : " (forward, down air)"));

    setAndVerify(MASTER_ID, false, function () {
      log("O1 open. Coasting " + (DWELL_MS / 1000) + " s.");
      Timer.set(DWELL_MS, false, function () {
        setAndVerify(DIR_ID, newDir, function () {
          var proceed = function () {
            Timer.set(SETTLE_MS, false, function () {
              setAndVerify(MASTER_ID, true, function () {
                log("O1 closed. Reversal complete. Supervision grace started.");
                busy = false;
              });
            });
          };
          if (newDir) { verifyCoil(proceed); } else { proceed(); }
        });
      });
    });
  });
}

// ---------- power supervision ----------
Shelly.addStatusHandler(function (ev) {
  // restart the grace window whenever O1 turns on, and rearm after a fault
  if (ev.component === "switch:" + MASTER_ID && ev.delta &&
      typeof ev.delta.output === "boolean") {
    if (ev.delta.output === true) {
      graceUntil = Date.now() + GRACE_MS;
      badPolls = 0;
      if (fault) { fault = false; publishFault(false); log("Rearmed by O1 on."); }
    }
  }
});

Timer.set(POLL_MS, true, function () {
  if (busy || fault) return;
  Shelly.call("Switch.GetStatus", { id: MASTER_ID }, function (st, err) {
    if (err !== 0 || !st.output) { badPolls = 0; return; }
    if (Date.now() < graceUntil) return;
    var p = st.apower;
    if (typeof p !== "number") return;
    if (p > STALL_W) {
      badPolls++;
      if (badPolls >= TRIP_COUNT) tripFault("power " + p + " W above stall ceiling " + STALL_W + " W.");
    } else if (p < MIN_RUN_W) {
      badPolls++;
      if (badPolls >= TRIP_COUNT) tripFault("power " + p + " W below running floor " + MIN_RUN_W + " W. Dial too low or fans disconnected.");
    } else {
      badPolls = 0;
    }
  });
});

// ---------- virtual button ----------
function hookButton() {
  Shelly.addEventHandler(function (ev) {
    if (ev.component === "button:" + BUTTON_ID && ev.info && ev.info.event === "single_push") {
      doReverse();
    }
  });
  log("Ready. Thresholds MIN_RUN_W=" + MIN_RUN_W + " STALL_W=" + STALL_W +
      " COIL_MIN_W=" + COIL_MIN_W + " . Commission before trusting.");
}

Shelly.call("Button.GetStatus", { id: BUTTON_ID }, function (r, err) {
  if (err === 0) { hookButton(); return; }
  Shelly.call("Virtual.Add",
    { type: "button", id: BUTTON_ID, config: { name: "Reverse fans" } },
    function (r2, e2, m2) {
      if (e2 !== 0) log("Virtual button create failed: " + m2 + " Use RPC Button.Trigger.");
      hookButton();
    });
});

// ---------- virtual boolean (fault, read by Home Assistant) ----------
Shelly.call("Boolean.GetStatus", { id: FAULT_BOOL_ID }, function (r, err) {
  if (err === 0) { publishFault(fault); return; }
  Shelly.call("Virtual.Add",
    { type: "boolean", id: FAULT_BOOL_ID,
      config: { name: "Fan fault", meta: { ui: { view: "label" } } } },
    function (r2, e2, m2) {
      if (e2 !== 0) { log("Virtual boolean create failed: " + m2); return; }
      publishFault(fault);  // reflect the current state on boot
    });
});
