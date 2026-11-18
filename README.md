# Health Auto Export Plugin for Indigo

An Indigo plugin that receives health and fitness data from the [Health Auto Export](https://www.healthexportapp.com) iOS app and exposes it as Indigo devices for automations, triggers, and control pages.

The plugin runs an embedded HTTP server that listens for JSON POSTs from Health Auto Export's REST API automations. Data is routed to typed device categories, each with its own device class and state schema.

## Features

- **Workouts**: rolling history (up to 20 most recent), de-duplicated by workout ID, with energy, duration, heart rate, intensity, distance, elevation gain, location, Apple effort score, temperature and humidity per workout
- **Activity rings**: three-day rolling window for active energy, Move/Exercise/Stand, steps, distances, VO2 max, running form metrics and more, with configurable daily targets and percentage completion states
- **Sleep stages**: total sleep (plus a ready-made `7h 15m` display state), core, deep, REM, awake, in bed, sleep start/end times and breathing disturbances
- **Nutrition**: daily totals for around 40 nutrients, with optional targets (energy, protein, carbs, fat, sodium, water) and percent-of-target states
- **Body, heart, respiratory, vitals, other**: three-day history plus "latest known value" states, including heart rate min/avg/max, blood pressure, respiratory rate, AFib burden and mindful minutes
- **Event devices**: State of Mind, ECG and heart rate notifications, medication doses, and symptoms
- **Midnight rollover**: activity targets shift and "today" counters reset at local midnight
- **Multi-user support**: devices subscribe to an `X-User-ID`, so one Indigo instance can track several people
- **Optional auth token** on the HTTP endpoint
- **Unmapped metric logging**: anything the plugin doesn't recognise is logged once, so new Health Auto Export metrics are easy to spot

## Requirements

- Indigo 2022.2 or later (Python 3 / Server API 3.4)
- macOS host running Indigo
- iOS device with Health Auto Export (Premium is needed for REST API automations)
- Network connectivity from the iOS device to the Indigo host on the configured port (default 8888)

## Installation

1. Download the latest release from the [Releases page](../../releases)
2. Double-click the `.indigoPlugin` bundle to install
3. Enable the plugin in Indigo
4. Open the plugin config and set the HTTP port (default `8888`) and, optionally, an auth token

On startup the Indigo log shows the exact URL to use, including your server's IP address.

## Setup

### Indigo side

1. Create a new device using `Plugins → Health Auto Export → New Device`
2. Choose the device type (see the table below)
3. Set the **User ID** (e.g. `Mike`). This must match what Health Auto Export sends
4. For Activity devices, optionally set target calories / workout minutes / stand hours (numbers or Indigo variable names)
5. For Nutrition devices, optionally set nutrient targets (numbers or Indigo variable names)
6. For Workouts devices, set how many workouts to keep in rolling history (1–20)

### iOS side

Health Auto Export sends **one data type per automation**, so create a REST API automation for each data type you want:

1. **Automations → New Automation → REST API**
2. URL: the one shown in the Indigo log, e.g. `http://192.168.1.50:8888/health`
3. Add an HTTP header `X-User-ID` with your user identifier, or append `?userId=Mike` to the URL
4. If you set an auth token, add the header `Authorization` with value `Bearer YOUR_TOKEN`
5. Export Format **JSON**, Export Version **2**
6. For Health Metrics: **Summarize Data on**, Time Grouping **Day**, Date Range **Default**

| Automation data type | Indigo device(s) |
|---|---|
| Health Metrics | Activity, Nutrition, Body Measurements, Heart, Respiratory, Sleep, Vitals, Other |
| Workouts | Workouts |
| State of Mind (iOS 18+) | State of Mind |
| ECG | Heart Events |
| Heart Rate Notifications | Heart Events |
| Medications (iOS 26+) | Medications |
| Symptoms | Symptoms |

## Architecture

Health Auto Export typically sends large bundles, often hundreds of metrics across many categories in a single POST. The plugin keeps Indigo responsive by:

- Accepting POSTs on a background HTTP server thread that does nothing but authenticate, JSON-parse and enqueue
- Draining the queue on the main plugin thread, so every Indigo API call happens on the thread Indigo expects
- Batching state writes with `updateStatesOnServer([...])` so each device gets one IPC call per update
- Discarding bulky arrays (workout routes, per-second series, ECG voltage traces) before anything is persisted in device props

## Device Types

| Type | Purpose | Key states |
|---|---|---|
| Workouts | Rolling workout history | `workout01_*` … `workout20_*`: name, start/end, duration, energy, HR avg/min/max, distance, elevation, location, effort score, source |
| Activity | Daily activity rings | `day01-03` actuals + targets + percent-complete; steps, distances, Move time, running/cycling metrics |
| Nutrition | Daily nutrient totals | `day01-03<Nutrient>`, `target<Nutrient>`, `day01-03<Nutrient>Percent`, `units` |
| Body Measurements | Weight, BMI, body fat, wrist temperature | `latest*` and `day01-03*` |
| Heart | HR min/avg/max, HRV, BP, resting/walking HR, AFib burden | `latest*` and `day01-03*` |
| Respiratory | Respiratory rate, SpO2, FEV, FVC, peak flow, inhaler usage | `latest*` and `day01-03*` |
| Sleep | Sleep stages, start/end, breathing disturbances | `latest*` and `day01-03*`, e.g. `day01TotalSleepDisplay` |
| Vitals | Body temp, blood glucose, insulin, perfusion | `latest*` and `day01-03*` |
| Other | Daylight, mindful minutes, UV, handwashing, alcohol, etc. | `latest*` and `day01-03*` |
| State of Mind | Mood and emotion logging | `lastClassification`, `lastValence`, `lastLabels`, `todayEntryCount`, `todayAverageValence` |
| Heart Events | ECG + HR notifications | `lastEcgClassification`, `lastNotificationType`, `lastNotificationPeakHeartRate`, today counts |
| Medications | Dose events | `lastDoseName`, `lastDoseStatus`, `todayTaken`, `todaySkipped` |
| Symptoms | Logged symptoms | `lastSymptomName`, `lastSymptomSeverity`, `todaySymptoms` |

## Actions & Menu Items

- **Clear All Data**: wipes all history and resets every state on a chosen device
- **Clear Day Data**: clears `day01`, `day02`, or `day03` from an activity, nutrition or measurement device
- **Log Unmapped Metrics** (menu): lists any metric names received that the plugin doesn't handle
- **Toggle Debugging** (menu)

Both actions require a confirmation checkbox before they run.

## Triggers

Any state change can drive an Indigo trigger. Useful patterns:

- New workout posted → notify, log, or update a control page
- `day01PercentCalories` crossing 100 → flash a light, send a notification
- `lastNotificationType` changes → alert someone about a high/low heart rate notification
- `day01ProteinPercent` or `day01WaterPercent` against a target → reminders
- `day01TotalSleep` below a threshold → gentler morning lighting scene

## Configuration

| Option | Default | Notes |
|---|---|---|
| HTTP Port | 8888 | Port the embedded server listens on |
| Auth Token | (blank) | If set, requests need `Authorization: Bearer TOKEN`, `X-Auth-Token: TOKEN` or `?token=TOKEN` |
| Show debug info | off | Verbose logging; leave off in normal use |

Per-device:

| Option | Devices | Notes |
|---|---|---|
| User ID | All | Must match `X-User-ID` header or `?userId=` query param |
| Max Workouts | Workouts | 1–20, rolling buffer size |
| Target Calories / Workout / Stand | Activity | Integer or Indigo variable name |
| Nutrient targets | Nutrition | Number or Indigo variable name; blank = no target |
| Fill Gaps | Measurement | If on, days without a measurement use the latest known value |

## Privacy & Network

The plugin's HTTP server binds to `0.0.0.0` so it can receive POSTs from any device on your LAN. Recommended setup:

- Set an **Auth Token** so only your Health Auto Export automations are accepted (rejected requests are logged)
- Keep the plugin's port behind your home firewall; don't expose it to the internet
- If you need remote access, terminate it on a reverse proxy with auth and forward to the plugin internally

No data leaves your Indigo host; everything is local.

## License

MIT License. See [LICENSE](LICENSE).

## Acknowledgements

Built for [Indigo](https://www.indigodomo.com) by Perceptive Automation. The Health Auto Export iOS app is the work of [Lybron Sobers](https://www.healthexportapp.com).
