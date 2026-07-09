# Publishing the Shelly fan fault as a virtual boolean

The supervision script latches its fault in a script variable (`fault`). The
version shipped here — [`fan_reverse_supervised.js`](fan_reverse_supervised.js)
— **already mirrors that fault to a virtual boolean** so Home Assistant can read
it as an entity instead of inferring it from an unexpected master-off.

## Flashing

Copy [`fan_reverse_supervised.js`](fan_reverse_supervised.js) into the Shelly Pro
2PM (Scripts → the existing fan script → replace the contents → Save → Start, or
enable "Run on startup"). On first run it creates two virtual components:

- **`button:200`** "Reverse fans" — the safe-reversal trigger.
- **`boolean:201`** "Fan fault" — mirrors the latched fault.

Remember to set the three `*_W` commissioning thresholds before trusting the
supervision.

## What was added (vs. the plain reversal script)

```js
var FAULT_BOOL_ID = 201;                 // virtual boolean id (button is 200)

function publishFault(v) {               // mirror the latch to the boolean
  Shelly.call("Boolean.Set", { id: FAULT_BOOL_ID, value: v });
}
```

- `tripFault()` calls `publishFault(true)` when it latches.
- The status handler calls `publishFault(false)` when O1 is turned back on (the
  rearm), so the boolean clears itself the moment the fault is cleared.
- A boot block creates `boolean:201` if missing and publishes the current state.

The boolean is created read-only (`meta.ui.view: "label"`), so pick an id that is
free on your device (201 by default; change `FAULT_BOOL_ID` if it clashes).

## In Home Assistant

The Shelly integration surfaces this virtual boolean as a `binary_sensor` (or a
`switch`, depending on firmware). Map it in the integration's **Ceiling fans &
cooling** step under **Fan fault** — the integration reads any `on`/`off` entity
the same way. Once mapped, a published `on` latches the integration's fault,
refuses to run the fans and notifies; re-arm as usual (turn the Shelly master on,
then toggle *Ceiling fans enabled* off→on). Until you map it, the integration
falls back to inferring a fault from an unexpected master-off.
