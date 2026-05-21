# Health Auto Export Plugin for Indigo

An Indigo plugin that receives health and fitness data from the [Health Auto Export](https://www.healthexportapp.com) iOS app and exposes it as Indigo devices for automations, triggers, and control pages.

The plugin runs an embedded HTTP server that listens for JSON POSTs from Health Auto Export's REST API export. Data is routed to typed device categories — workouts, daily activity, body measurements, heart, respiratory, sleep, vitals, and other — each with its own device class and state schema.

## Features

- **Workouts** — rolling history (configurable, up to 20 most recent), with energy, duration, intensity, temperature, humidity, and heart rate statistics per workout
- **Activity rings** — three-day rolling window for active energy, exercise time, stand hours, steps, distances, VO2 max, audio exposure, and more, with configurable daily targets (calories / workout / stand) and automatic percentage completion states
- **Midnight rollover** — activity targets shift automatically at midnight so `day01` always means today
- **Body measurements, heart, respiratory, sleep, vitals, other** — separate device types for each category, each with three-day history plus "latest known value" states
- **Multi-user support** — different devices can subscribe to different `X-User-ID` values, so one Indigo instance can track multiple people
- **Per-day clear actions** — clear today, yesterday, or two days ago without losing the rest of the history
- **Variable-backed targets** — activity targets can be hard-coded integers or the name of an Indigo variable, useful for adjusting goals across the house from a single source
- **HomeKit-friendly device types** — sensor-based devices that bridge cleanly through the HomeKit bridge plugin

## Requirements

- Indigo 2022.2 or later (Python 3 / Server API 3.4)
- macOS host running Indigo
- iOS device with the Health Auto Export app installed
- Network connectivity from the iOS device to the Indigo host on the configured port (default 8888)

## Installation

1. Download the latest release from the [Releases page](../../releases)
2. Double-click the `.indigoPlugin` bundle to install
3. Enable the plugin in Indigo
4. Open the plugin config and set the HTTP port (default `8888`)

## Setup

### Indigo side

1. Create a new device using `Plugins → Health Auto Export → New Device`
2. Choose the device type — Workouts, Activity, or one of the measurement categories
3. Set the **User ID** (e.g. `Mike`) — this must match what you send from Health Auto Export
4. For Activity devices, optionally set target calories / workout minutes / stand hours (numbers or Indigo variable names)
5. For Workouts devices, set how many workouts to keep in rolling history (1–20)

### iOS side

In the Health Auto Export app:

1. Create a new automation under **Automations → REST API**
2. Set the URL to `http://YOUR_INDIGO_HOST:8888/health`
3. Add an HTTP header `X-User-ID` with your user identifier — or append `?userId=Mike` to the URL
4. Choose the data types to export (workouts, activity metrics, body measurements, etc.)
5. Set the export frequency (manual, daily, after workouts, etc.)

The same Indigo instance can receive data for multiple users — just create one set of devices per user and use distinct `X-User-ID` values from each iOS device.

## Architecture

Health Auto Export typically sends large bundles — hundreds of metrics across many categories in a single POST. The plugin keeps Indigo responsive by:

- Accepting POSTs on a background HTTP server thread that does nothing but JSON-parse and enqueue
- Draining the queue on the main plugin thread, so every Indigo API call happens on the thread Indigo expects
- Batching state writes with `updateStatesOnServer([...])` so each device gets one IPC call per update instead of dozens

This matters because a typical sync containing workouts, activity, and several measurement categories can otherwise generate 400+ individual state writes — enough to lock up `indigoserver` if any of them race with main-thread work.

## Device Types

| Type | Purpose | Key states |
|---|---|---|
| Workouts | Rolling workout history | `workout01_*` through `workoutNN_*` (name, duration, energy, HR avg/min/max, intensity, temperature, humidity) |
| Activity | Daily activity rings | `day01-03` actuals + targets + percent-complete for calories, exercise time, stand hours; plus steps, distances, audio exposure, etc. |
| Body Measurements | Weight, BMI, body fat, etc. | `latest*` and `day01-03*` values |
| Heart | HR, HRV, BP, notifications | `latest*` and `day01-03*` values |
| Respiratory | SpO2, FEV, FVC, inhaler usage | `latest*` and `day01-03*` values |
| Sleep | Sleep analysis | `latest*` and `day01-03*` values |
| Vitals | Body temp, blood glucose, insulin | `latest*` and `day01-03*` values |
| Other | UV, daylight, handwashing, alcohol, etc. | `latest*` and `day01-03*` values |

## Actions

- **Clear All Data** — wipes all history and resets all states for a chosen device
- **Clear Day Data** — clears `day01`, `day02`, or `day03` from an activity or measurement device

Both actions require a confirmation checkbox before they run.

## Triggers

Any state change can drive an Indigo trigger. Useful patterns:

- New workout posted → notify, log, or update a control page
- `day01PercentCalories` crossing 100 → flash a light, send a notification
- `latestBloodPressureSystolic` above a threshold → send an alert
- Step count below a target by a given hour → reminder to move

## Configuration

| Option | Default | Notes |
|---|---|---|
| HTTP Port | 8888 | Port the embedded server listens on |
| Show debug info | off | Verbose logging — leave off in normal use |

Per-device:

| Option | Devices | Notes |
|---|---|---|
| User ID | All | Must match `X-User-ID` header or `?userId=` query param |
| Max Workouts | Workouts | 1–20, rolling buffer size |
| Target Calories / Workout / Stand | Activity | Integer or Indigo variable name |
| Fill Gaps | Measurement | If on, days without a measurement use the latest known value |

## Privacy & Network

The plugin's HTTP server binds to `0.0.0.0` so it can receive POSTs from any device on your LAN. There is no authentication on the endpoint — anyone on your network could submit health data if they knew the port and user ID. Recommended setup:

- Keep the plugin's port behind your home firewall (don't expose it to the internet)
- Use a non-default port if you want a small obscurity hedge
- If you need remote access, terminate that on a reverse proxy with auth and forward to the plugin internally

No data leaves your Indigo host — everything is local.

## License

MIT License. See [LICENSE](LICENSE).

## Acknowledgements

Built for [Indigo](https://www.indigodomo.com) by Perceptive Automation. The Health Auto Export iOS app is the work of [Lybron Sobers](https://www.healthexportapp.com).
