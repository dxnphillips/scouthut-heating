# Publishing the Shelly fan fault as a virtual boolean

The supervision script
([`fan_reverse_supervised.js`](../../)) latches its fault in a script variable
(`fault`) and is **not** currently exposed to Home Assistant. Until it is, this
integration *infers* a fault from an unexpected master-off. That works, but a
real published flag is cleaner and unambiguous.

Add a **virtual boolean** to the Shelly and keep it in step with `fault`. Then
map it in the integration's *Ceiling fans & cooling* step (the **Fan fault**
field) and the inference is bypassed.

## Script changes

Create the boolean once at start-up (id 200 is free on a Pro 2PM; pick another
if it clashes) and push the fault to it wherever `fault` changes:

```js
// ---- add near the other config ----
var FAULT_BOOL_ID = 200;   // virtual boolean component id

// ---- add this helper ----
function publishFault(v) {
  Shelly.call("Boolean.Set", { id: FAULT_BOOL_ID, value: v });
}

// ---- create the boolean at start-up (put beside the button bootstrap) ----
Shelly.call("Boolean.GetStatus", { id: FAULT_BOOL_ID }, function (r, err) {
  if (err !== 0) {
    Shelly.call("Virtual.Add", {
      type: "boolean", id: FAULT_BOOL_ID,
      config: { name: "Fan fault", meta: { ui: { view: "label" } } }
    });
  }
  publishFault(fault);   // reflect current state on boot
});
```

Then set it in the two places the fault flips:

```js
function tripFault(reason) {
  fault = true;
  publishFault(true);                 // <-- add
  log("FAULT LATCHED: " + reason + " O1 opened. Investigate, then switch O1 on to rearm.");
  Shelly.call("Switch.Set", { id: MASTER_ID, on: false });
}
```

```js
// inside the status handler, where the fault is cleared on O1 turning on:
if (fault) { fault = false; publishFault(false); log("Rearmed by O1 on."); }
```

## In Home Assistant

The Shelly integration surfaces a virtual boolean as a `binary_sensor` (with the
read-only `view: "label"` above) or as a `switch`. Either can be mapped in the
**Fan fault** field — the integration reads any `on`/`off` entity the same way.
Once mapped, a published `on` latches the integration's fault, refuses to run the
fans and notifies; re-arm as usual (turn the Shelly master on, then toggle
*Ceiling fans enabled* off→on).
