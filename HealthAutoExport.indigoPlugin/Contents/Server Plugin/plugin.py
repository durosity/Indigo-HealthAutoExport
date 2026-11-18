#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import indigo
import json
import copy
from datetime import datetime, timedelta, timezone
import traceback
import threading
import queue
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import urllib.parse
import hmac
import socket


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request on its own thread so a slow
    main-thread queue drain doesn't block new incoming POSTs."""
    daemon_threads = True
    allow_reuse_address = True


class HealthDataHandler(BaseHTTPRequestHandler):
    """HTTP request handler for health data.

    IMPORTANT: This handler runs on a worker thread. It MUST NOT touch the
    Indigo API directly. It only validates and enqueues; all Indigo work is
    done by runConcurrentThread on the main plugin thread.
    """

    def log_message(self, format, *args):
        """Suppress default HTTP server logging"""
        pass

    def do_POST(self):
        """Handle POST requests - enqueue only, do NOT call Indigo API here"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            # Cap at 50MB to defend against bad/huge payloads
            if content_length > 50 * 1024 * 1024:
                self.send_response(413)
                self.end_headers()
                self.wfile.write(b'{"error": "Payload too large"}')
                return

            # Optional shared-secret auth (set in plugin config)
            plugin = getattr(self.server, 'plugin', None)
            expected = getattr(plugin, 'auth_token', '') if plugin else ''
            if expected:
                parsed_auth = urllib.parse.urlparse(self.path)
                supplied = (
                    self.headers.get('X-Auth-Token', '')
                    or self.headers.get('Authorization', '').replace('Bearer ', '', 1).strip()
                    or urllib.parse.parse_qs(parsed_auth.query).get('token', [''])[0]
                )
                if not hmac.compare_digest(str(supplied), str(expected)):
                    # Drain body so the client sees a clean response
                    try:
                        self.rfile.read(content_length)
                    except Exception:
                        pass
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error": "Unauthorized"}')
                    if plugin:
                        plugin.note_auth_failure(self.client_address[0])
                    return

            post_data = self.rfile.read(content_length)

            try:
                data = json.loads(post_data.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "Invalid JSON"}')
                return

            # Get user ID from headers or URL params
            user_id = self.headers.get('X-User-ID', 'default')
            parsed_path = urllib.parse.urlparse(self.path)
            query_params = urllib.parse.parse_qs(parsed_path.query)
            if 'userId' in query_params:
                user_id = query_params['userId'][0]

            # Health Auto Export sends these headers on every REST request
            meta = {
                'automationName': self.headers.get('automation-name', ''),
                'automationPeriod': self.headers.get('automation-period', ''),
                'automationAggregation': self.headers.get('automation-aggregation', ''),
                'sessionId': self.headers.get('session-id', ''),
            }

            # Hand off to the plugin via the queue; main thread will process
            if hasattr(self.server, 'plugin'):
                self.server.plugin.enqueue_health_data(user_id, data, meta)

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "queued"}')

        except Exception as e:
            try:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            except Exception:
                pass

    def do_GET(self):
        """Handle GET requests"""
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"status": "Health Auto Export Plugin Running"}')


class Plugin(indigo.PluginBase):

    ########################################
    # Metric name -> state name mappings
    #
    # Names come from the Health Auto Export JSON format and the official
    # HealthyApps server's MetricName enum. Older/alternative spellings are
    # kept as aliases so existing exports keep working.
    ########################################

    # Activity (daily totals / daily averages)
    METRIC_MAPPING = {
        'active_energy': 'ActiveEnergyBurned',
        'basal_energy': 'BasalEnergyBurned',
        'basal_energy_burned': 'BasalEnergyBurned',
        'apple_exercise_time': 'AppleExerciseTime',
        'apple_move_time': 'AppleMoveTime',
        'apple_stand_hour': 'AppleStandHour',
        'apple_stand_time': 'AppleStandTime',
        'flights_climbed': 'FlightsClimbed',
        'step_count': 'StepCount',
        'distance_walking_running': 'WalkingRunningDistance',
        'walking_running_distance': 'WalkingRunningDistance',
        'distance_cycling': 'CyclingDistance',
        'cycling_distance': 'CyclingDistance',
        'distance_swimming': 'SwimmingDistance',
        'swimming_distance': 'SwimmingDistance',
        'distance_wheelchair': 'WheelchairDistance',
        'wheelchair_distance': 'WheelchairDistance',
        'push_count': 'WheelchairPushCount',
        'wheelchair_push_count': 'WheelchairPushCount',
        'distance_downhill_snow_sports': 'DownhillSnowSportsDistance',
        'downhill_snow_sports': 'DownhillSnowSportsDistance',
        'vo2_max': 'VO2Max',
        'vo2max': 'VO2Max',
        'cycling_cadence': 'CyclingCadence',
        'cycling_speed': 'CyclingSpeed',
        'cycling_power': 'CyclingPower',
        'cycling_functional_threshold_power': 'CyclingFunctionalThresholdPower',
        'running_speed': 'RunningSpeed',
        'running_power': 'RunningPower',
        'running_ground_contact_time': 'RunningGroundContactTime',
        'running_stride_length': 'RunningStrideLength',
        'running_vertical_oscillation': 'RunningVerticalOscillation',
        'swimming_stroke_count': 'SwimmingStrokeCount',
        'swim_stroke_count': 'SwimmingStrokeCount',
        'physical_effort': 'PhysicalEffort',
        'underwater_depth': 'UnderwaterDepth',
        'under_water_depth': 'UnderwaterDepth',
        'heart_rate': 'HeartRate',
        'heart_rate_variability': 'HeartRateVariability',
        'resting_heart_rate': 'RestingHeartRate',
        'walking_heart_rate_average': 'WalkingHeartRateAverage',
        'walking_speed': 'WalkingSpeed',
        'walking_step_length': 'WalkingStepLength',
        'walking_asymmetry_percentage': 'WalkingAsymmetryPercentage',
        'walking_double_support_percentage': 'WalkingDoubleSupportPercentage',
        'six_minute_walking_test_distance': 'SixMinuteWalkingTestDistance',
        'stair_speed_up': 'StairSpeedUp',
        'stair_speed_down': 'StairSpeedDown',
        'headphone_audio_exposure': 'HeadphoneAudioExposure',
        'headphone_audio': 'HeadphoneAudioExposure',
        'environmental_audio_exposure': 'EnvironmentalAudioExposure',
        'environmental_audio': 'EnvironmentalAudioExposure',
    }

    # Activity states that are rates/levels: averaged across a day, not summed
    ACTIVITY_AVERAGE_STATES = {
        'VO2Max', 'CyclingCadence', 'CyclingSpeed', 'CyclingPower',
        'CyclingFunctionalThresholdPower', 'RunningSpeed', 'RunningPower',
        'RunningGroundContactTime', 'RunningStrideLength',
        'RunningVerticalOscillation', 'PhysicalEffort', 'HeartRate',
        'HeartRateVariability', 'RestingHeartRate', 'WalkingHeartRateAverage',
        'WalkingSpeed', 'WalkingStepLength', 'WalkingAsymmetryPercentage',
        'WalkingDoubleSupportPercentage', 'StairSpeedUp', 'StairSpeedDown',
        'HeadphoneAudioExposure', 'EnvironmentalAudioExposure',
        'SixMinuteWalkingTestDistance', 'UnderwaterDepth',
    }

    BODY_METRIC_MAPPING = {
        'weight_body_mass': 'BodyMass',
        'body_fat_percentage': 'BodyFatPercentage',
        'body_mass': 'BodyMass',
        'body_mass_index': 'BodyMassIndex',
        'height': 'Height',
        'lean_body_mass': 'LeanBodyMass',
        'waist_circumference': 'WaistCircumference',
        'wrist_temperature': 'WristTemperature',
        'apple_sleeping_wrist_temperature': 'WristTemperature',
        'weight_body_fat_percentage': 'BodyFatPercentage',
        'weight_body_mass_index': 'BodyMassIndex',
        'weight_height': 'Height',
        'weight_lean_body_mass': 'LeanBodyMass',
        'weight_waist_circumference': 'WaistCircumference',
        'weight_wrist_temperature': 'WristTemperature',
    }

    HEART_METRIC_MAPPING = {
        'cardio_recovery': 'CardioRecovery',
        'blood_pressure': 'BloodPressureSystolic',          # special: also writes Diastolic
        'blood_pressure_systolic': 'BloodPressureSystolic',
        'blood_pressure_diastolic': 'BloodPressureDiastolic',
        'heart_rate': 'HeartRate',                          # special: Min/Avg/Max
        'heart_rate_variability': 'HeartRateVariability',
        'recovery_hrv': 'RecoveryHRV',                      # iOS 27 RMSSD - name unverified
        'heart_rate_variability_rmssd': 'RecoveryHRV',
        'atrial_fibrillation_burden': 'AtrialFibrillationBurden',
        'high_heart_rate_notifications': 'HighHeartRateNotifications',
        'irregular_heart_rate_notifications': 'IrregularHeartRateNotifications',
        'low_heart_rate_notifications': 'LowHeartRateNotifications',
        'resting_heart_rate': 'RestingHeartRate',
        'walking_average_heart_rate': 'WalkingAverageHeartRate',
        'walking_heart_rate': 'WalkingAverageHeartRate',
    }

    RESPIRATORY_METRIC_MAPPING = {
        'respiratory_rate': 'RespiratoryRate',
        'blood_oxygen_saturation': 'BloodOxygenSaturation',
        'forced_expiratory_volume': 'ForcedExpiratoryVolume',
        'forced_vital_capacity': 'ForcedVitalCapacity',
        'peak_expiratory_flow_rate': 'PeakExpiratoryFlowRate',
        'inhaler_usage': 'InhalerUsage',
    }

    SLEEP_METRIC_MAPPING = {
        'sleep_analysis': 'SleepAnalysis',                  # special: stages
        'breathing_disturbances': 'BreathingDisturbances',
    }

    VITALS_METRIC_MAPPING = {
        'basal_body_temperature': 'BasalBodyTemperature',
        'blood_glucose': 'BloodGlucose',
        'body_temperature': 'BodyTemperature',
        'insulin_delivery': 'InsulinDelivery',
        'peripheral_perfusion': 'PeripheralPerfusion',
        'peripheral_perfusion_index': 'PeripheralPerfusion',
    }

    OTHER_METRIC_MAPPING = {
        'alcohol_consumption': 'AlcoholConsumption',
        'number_of_alcoholic_beverages': 'AlcoholConsumption',
        'blood_alcohol_content': 'BloodAlcoholContent',
        'electrodermal_activity': 'ElectrodermalActivity',
        'handwashing': 'Handwashing',
        'mindful_minutes': 'MindfulMinutes',
        'mindful_session': 'MindfulMinutes',
        'number_of_times_fallen': 'NumberOfTimesFallen',
        'number_of_time_fallen': 'NumberOfTimesFallen',
        'time_in_daylight': 'TimeInDaylight',
        'toothbrushing': 'Toothbrushing',
        'underwater_temperature': 'UnderwaterTemperature',
        'under_water_temperature': 'UnderwaterTemperature',
        'uv_exposure': 'UVExposure',
    }

    # Nutrition (daily totals). 'dietary_' prefixed variants are matched
    # automatically by _lookup_metric.
    NUTRITION_METRIC_MAPPING = {
        'dietary_energy': 'DietaryEnergy',
        'protein': 'Protein',
        'carbohydrates': 'Carbohydrates',
        'total_fat': 'TotalFat',
        'saturated_fat': 'SaturatedFat',
        'monosaturated_fat': 'MonounsaturatedFat',
        'monounsaturated_fat': 'MonounsaturatedFat',
        'polyunsaturated_fat': 'PolyunsaturatedFat',
        'cholesterol': 'Cholesterol',
        'fiber': 'Fiber',
        'dietary_sugar': 'Sugar',
        'sugar': 'Sugar',
        'dietary_water': 'Water',
        'water': 'Water',
        'caffeine': 'Caffeine',
        'sodium': 'Sodium',
        'potassium': 'Potassium',
        'chloride': 'Chloride',
        'calcium': 'Calcium',
        'phosphorus': 'Phosphorus',
        'magnesium': 'Magnesium',
        'iron': 'Iron',
        'zinc': 'Zinc',
        'copper': 'Copper',
        'manganese': 'Manganese',
        'selenium': 'Selenium',
        'iodine': 'Iodine',
        'chromium': 'Chromium',
        'molybdenum': 'Molybdenum',
        'biotin': 'Biotin',
        'folate': 'Folate',
        'niacin': 'Niacin',
        'pantothenic_acid': 'PantothenicAcid',
        'riboflavin': 'Riboflavin',
        'thiamin': 'Thiamin',
        'vitamin_a': 'VitaminA',
        'vitamin_b6': 'VitaminB6',
        'vitamin_b12': 'VitaminB12',
        'vitamin_c': 'VitaminC',
        'vitamin_d': 'VitaminD',
        'vitamin_e': 'VitaminE',
        'vitamin_k': 'VitaminK',
    }

    # Nutrition targets: config field -> state name
    NUTRITION_TARGETS = {
        'targetEnergy': 'DietaryEnergy',
        'targetProtein': 'Protein',
        'targetCarbohydrates': 'Carbohydrates',
        'targetFat': 'TotalFat',
        'targetSodium': 'Sodium',
        'targetWater': 'Water',
    }

    # How measurement-device states are combined within a single day.
    # Anything not listed uses "latest" (most recent reading wins).
    MEASUREMENT_AGGREGATION = {
        'HeartRate': 'mean',
        'HeartRateMin': 'min',
        'HeartRateMax': 'max',
        'HeartRateVariability': 'mean',
        'RespiratoryRate': 'mean',
        'BloodOxygenSaturation': 'mean',
        'TimeInDaylight': 'sum',
        'MindfulMinutes': 'sum',
        'InhalerUsage': 'sum',
        'NumberOfTimesFallen': 'sum',
        'AlcoholConsumption': 'sum',
        'InsulinDelivery': 'sum',
    }

    # States that hold strings rather than numbers
    STRING_STATES = {'SleepStart', 'SleepEnd', 'InBedStart', 'InBedEnd', 'TotalSleepDisplay'}

    # Sleep stage fields (summarised export) -> state names
    SLEEP_STAGE_FIELDS = {
        'totalSleep': 'TotalSleep',
        'asleep': 'AsleepUnspecified',
        'core': 'CoreSleep',
        'deep': 'DeepSleep',
        'rem': 'REMSleep',
        'awake': 'AwakeTime',
        'inBed': 'InBedTime',
    }
    # Sleep segment "value" (unsummarised export) -> state names
    SLEEP_SEGMENT_VALUES = {
        'Core': 'CoreSleep',
        'Deep': 'DeepSleep',
        'REM': 'REMSleep',
        'Asleep': 'AsleepUnspecified',
        'Awake': 'AwakeTime',
        'In Bed': 'InBedTime',
    }

    # Device-type -> mapping, used by dispatch and clear actions
    MEASUREMENT_DEVICE_TYPES = (
        "bodyMeasurements", "heart", "respiratory",
        "sleep", "vitals", "other",
    )

    EVENT_DEVICE_TYPES = ("stateOfMind", "heartEvents", "medications", "symptoms")

    OWN_DEVICE_TYPES = (
        "workouts", "activity", "nutrition",
    ) + MEASUREMENT_DEVICE_TYPES + EVENT_DEVICE_TYPES

    EVENT_HISTORY_DAYS = 7
    EVENT_HISTORY_MAX = 100

    ########################################
    # Plugin lifecycle
    ########################################

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super(Plugin, self).__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = pluginPrefs.get("showDebugInfo", False)
        self.http_port = int(pluginPrefs.get("httpPort", "8888"))
        self.auth_token = str(pluginPrefs.get("authToken", "")).strip()
        self.http_server = None
        self.http_thread = None
        # Bounded queue; drops oldest if full.
        self.work_queue = queue.Queue(maxsize=200)
        self.next_midnight_check = None
        # Metric names received that we don't map (name -> count)
        self.unmapped_metrics = {}
        # Auth failures are counted on the HTTP thread, logged on main thread
        self._auth_failures = queue.Queue()

        self.measurement_mappings = {
            "bodyMeasurements": self.BODY_METRIC_MAPPING,
            "heart": self.HEART_METRIC_MAPPING,
            "respiratory": self.RESPIRATORY_METRIC_MAPPING,
            "sleep": self.SLEEP_METRIC_MAPPING,
            "vitals": self.VITALS_METRIC_MAPPING,
            "other": self.OTHER_METRIC_MAPPING,
        }

    def startup(self):
        self.logger.info(f"Starting Health Auto Export Plugin v{self.pluginVersion}")
        if self.auth_token:
            self.logger.info("Request authentication is ENABLED")
        self.start_http_server()
        self.schedule_midnight_check()

    def shutdown(self):
        self.logger.info("Shutting down Health Auto Export Plugin")
        self.stop_http_server()

    ########################################
    # Main thread work loop
    ########################################

    def runConcurrentThread(self):
        """Main plugin thread: drains the work queue (all Indigo API calls
        happen here) and handles midnight rollover."""
        try:
            while True:
                processed = 0
                while processed < 5:
                    try:
                        user_id, data, meta = self.work_queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self.process_health_data(user_id, data, meta)
                    except Exception as e:
                        self.logger.error(f"Unhandled error processing queued data: {e}")
                        self.logger.error(traceback.format_exc())
                    finally:
                        self.work_queue.task_done()
                    processed += 1

                # Log any rejected (unauthorised) requests
                while True:
                    try:
                        ip = self._auth_failures.get_nowait()
                    except queue.Empty:
                        break
                    self.logger.warning(f"Rejected unauthorised request from {ip}")

                if self.next_midnight_check and datetime.now() >= self.next_midnight_check:
                    self.logger.info("Midnight reached - rolling over daily targets and counters")
                    self.shift_activity_targets()
                    self.refresh_event_devices()
                    self.schedule_midnight_check()

                if self.work_queue.empty():
                    self.sleep(5)
                else:
                    self.sleep(0.1)

        except self.StopThread:
            pass

    def enqueue_health_data(self, user_id, data, meta=None):
        """Called from the HTTP worker thread. Thread-safe. Never touches
        the Indigo API."""
        item = (user_id, data, meta or {})
        try:
            self.work_queue.put_nowait(item)
        except queue.Full:
            try:
                self.work_queue.get_nowait()
                self.work_queue.task_done()
            except queue.Empty:
                pass
            try:
                self.work_queue.put_nowait(item)
            except queue.Full:
                pass

    def note_auth_failure(self, ip):
        """Called from HTTP thread; logged later on the main thread."""
        try:
            self._auth_failures.put_nowait(ip)
        except Exception:
            pass

    ########################################
    # Helpers
    ########################################

    @staticmethod
    def _local_today():
        """Today's date on the Indigo server, as YYYY-MM-DD.
        (Previously UTC, which mis-filed data between 00:00 and 01:00 BST.)"""
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _parse_dt(value):
        """Parse any timestamp format Health Auto Export emits into an aware
        datetime in local time. Returns None if unparseable."""
        if value is None or value == "":
            return None
        try:
            if isinstance(value, (int, float)):
                return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone()
            s = str(value).strip()
            try:
                return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z").astimezone()
            except ValueError:
                pass
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
        except Exception:
            return None

    @staticmethod
    def _fmt_dt(dt):
        return dt.strftime("%Y-%m-%d %H:%M") if dt else ""

    @staticmethod
    def _fmt_hours(hours):
        try:
            total_mins = int(round(float(hours) * 60))
        except Exception:
            return ""
        return f"{total_mins // 60}h {total_mins % 60:02d}m"

    @staticmethod
    def _num(value, default=0):
        try:
            if value is None or isinstance(value, bool):
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _pretty(text):
        """daily_mood -> Daily Mood, very_pleasant -> Very Pleasant"""
        return str(text or "").replace("_", " ").strip().title()

    @staticmethod
    def _qty_str(obj, decimals=1):
        """{'qty': 3.5, 'units': 'km'} -> '3.5 km'"""
        if not isinstance(obj, dict) or obj.get("qty") is None:
            return ""
        try:
            return f"{float(obj['qty']):.{decimals}f} {obj.get('units', '')}".strip()
        except Exception:
            return ""

    @staticmethod
    def _energy_to_kcal(qty, units):
        units = (units or "").lower()
        if units == "kj":
            return qty / 4.184
        return qty

    def _find_device(self, device_type, user_id):
        for dev in indigo.devices.iter(f"self.{device_type}"):
            if dev.pluginProps.get("userId", "") == user_id:
                return dev
        return None

    def _load_json_prop(self, dev, key, default):
        try:
            return json.loads(dev.pluginProps.get(key, json.dumps(default)))
        except Exception:
            return default

    def _save_props(self, dev, **items):
        new_props = dev.pluginProps
        for key, value in items.items():
            new_props[key] = json.dumps(value)
        dev.replacePluginPropsOnServer(new_props)

    def _lookup_metric(self, name):
        """Return (device_type, state_name) for a metric name, or (None, None)."""
        candidates = [name]
        if name.startswith("dietary_"):
            candidates.append(name[len("dietary_"):])
        else:
            candidates.append("dietary_" + name)

        for candidate in candidates:
            for device_type, mapping in self.measurement_mappings.items():
                if candidate in mapping:
                    return device_type, mapping[candidate]
            if candidate in self.NUTRITION_METRIC_MAPPING:
                return "nutrition", self.NUTRITION_METRIC_MAPPING[candidate]
            if candidate in self.METRIC_MAPPING:
                return "activity", self.METRIC_MAPPING[candidate]
        return None, None

    def _note_unmapped(self, metric_name, units="", kind="metric"):
        count = self.unmapped_metrics.get(metric_name, 0)
        self.unmapped_metrics[metric_name] = count + 1
        if count == 0:
            unit_str = f" ({units})" if units else ""
            self.logger.info(
                f"Received {kind} '{metric_name}'{unit_str} which this plugin doesn't handle yet "
                f"- ignored. (Logged once per session; see Plugins menu > Log Unmapped Metrics)"
            )

    ########################################
    # Midnight rollover
    ########################################

    def schedule_midnight_check(self):
        now = datetime.now()
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        self.next_midnight_check = next_midnight
        if self.debug:
            self.logger.debug(f"Next midnight check scheduled for: {next_midnight}")

    def resolve_target_value(self, config_value):
        """Resolve target value from config - could be a number or variable name"""
        if not config_value:
            return 0

        config_value = str(config_value).strip()

        try:
            return int(float(config_value))
        except ValueError:
            pass

        try:
            if config_value in indigo.variables:
                var_value = indigo.variables[config_value].value
                return int(float(var_value))
        except Exception:
            pass

        self.logger.warning(f"Could not resolve target value '{config_value}', defaulting to 0")
        return 0

    def shift_activity_targets(self):
        """Shift activity targets at midnight - day01 -> day02, day02 -> day03"""
        try:
            for dev in indigo.devices.iter("self.activity"):
                day01_cal = dev.states.get("day01TargetCalories", 0)
                day01_work = dev.states.get("day01TargetWorkout", 0)
                day01_stand = dev.states.get("day01TargetStand", 0)

                day02_cal = dev.states.get("day02TargetCalories", 0)
                day02_work = dev.states.get("day02TargetWorkout", 0)
                day02_stand = dev.states.get("day02TargetStand", 0)

                target_cal = self.resolve_target_value(dev.pluginProps.get("targetCalories", "0"))
                target_work = self.resolve_target_value(dev.pluginProps.get("targetWorkout", "0"))
                target_stand = self.resolve_target_value(dev.pluginProps.get("targetStand", "0"))

                updates = [
                    {"key": "day03TargetCalories", "value": day02_cal},
                    {"key": "day03TargetWorkout", "value": day02_work},
                    {"key": "day03TargetStand", "value": day02_stand},
                    {"key": "day02TargetCalories", "value": day01_cal},
                    {"key": "day02TargetWorkout", "value": day01_work},
                    {"key": "day02TargetStand", "value": day01_stand},
                    {"key": "day01TargetCalories", "value": target_cal},
                    {"key": "day01TargetWorkout", "value": target_work},
                    {"key": "day01TargetStand", "value": target_stand},
                ]
                dev.updateStatesOnServer(updates)

                dev = indigo.devices[dev.id]
                self.calculate_activity_percentages(dev)

                if self.debug:
                    self.logger.debug(f"Shifted targets for device: {dev.name}")

        except Exception as e:
            self.logger.error(f"Error shifting activity targets: {str(e)}")
            self.logger.error(traceback.format_exc())

    def refresh_event_devices(self):
        """Recompute 'today' counters on event devices so they reset at midnight."""
        for device_type in self.EVENT_DEVICE_TYPES:
            for dev in indigo.devices.iter(f"self.{device_type}"):
                try:
                    history = self._load_json_prop(dev, "eventHistory", [])
                    self.update_event_states(dev, device_type, history)
                except Exception as e:
                    self.logger.error(f"Error refreshing {dev.name}: {e}")

    ########################################
    # HTTP server lifecycle
    ########################################

    def start_http_server(self):
        """Start the embedded HTTP server"""
        try:
            self.http_server = _ThreadingHTTPServer(('0.0.0.0', self.http_port), HealthDataHandler)
            self.http_server.plugin = self

            self.http_thread = threading.Thread(target=self.http_server.serve_forever, name="HAE-HTTPServer")
            self.http_thread.daemon = True
            self.http_thread.start()

            self.logger.info(f"HTTP server started on port {self.http_port}")
            self.logger.info(f"Configure Auto Export to POST to: http://{self._server_ip()}:{self.http_port}/health")

        except Exception as e:
            self.logger.error(f"Failed to start HTTP server: {str(e)}")
            self.logger.error(traceback.format_exc())

    @staticmethod
    def _server_ip():
        """Best guess at this Mac's LAN IP address.

        Connecting a UDP socket sends no packets; it just asks the OS which
        interface/source address it would route through, which skips
        loopback and picks the primary network interface."""
        for target in ("192.0.2.1", "8.8.8.8"):
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.connect((target, 80))
                ip = sock.getsockname()[0]
                if ip and not ip.startswith("127."):
                    return ip
            except Exception:
                pass
            finally:
                if sock:
                    sock.close()
        try:
            ip = socket.gethostbyname(socket.gethostname())
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass
        return "YOUR_INDIGO_SERVER_IP"

    def stop_http_server(self):
        """Stop the embedded HTTP server"""
        if self.http_server:
            try:
                self.http_server.shutdown()
                self.http_server.server_close()
                self.logger.info("HTTP server stopped")
            except Exception:
                pass
            finally:
                self.http_server = None
                self.http_thread = None

    ########################################
    # Config / device callbacks
    ########################################

    def validatePrefsConfigUi(self, valuesDict):
        errorsDict = indigo.Dict()
        try:
            port = int(valuesDict.get("httpPort", "8888"))
            if port < 1024 or port > 65535:
                errorsDict["httpPort"] = "Port must be between 1024 and 65535"
        except ValueError:
            errorsDict["httpPort"] = "Port must be a number"
        if errorsDict:
            return (False, valuesDict, errorsDict)
        return (True, valuesDict)

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        """Called when plugin config dialog is closed"""
        if not userCancelled:
            self.debug = valuesDict.get("showDebugInfo", False)
            self.auth_token = str(valuesDict.get("authToken", "")).strip()
            new_port = int(valuesDict.get("httpPort", "8888"))

            if new_port != self.http_port:
                self.logger.info(f"HTTP port changed from {self.http_port} to {new_port}, restarting server")
                self.stop_http_server()
                self.http_port = new_port
                self.start_http_server()

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        """Validate device configuration"""
        errorsDict = indigo.Dict()

        if not valuesDict.get("userId", "").strip():
            errorsDict["userId"] = "User ID is required"

        if typeId == "workouts":
            try:
                max_workouts = int(valuesDict.get("maxWorkouts", "3"))
                if max_workouts < 1 or max_workouts > 20:
                    errorsDict["maxWorkouts"] = "Must be between 1 and 20"
            except ValueError:
                errorsDict["maxWorkouts"] = "Must be a valid number"

        if errorsDict:
            return (False, valuesDict, errorsDict)
        return (True, valuesDict)

    def _activity_targets_changed(self, origDev, newDev):
        keys = ("targetCalories", "targetWorkout", "targetStand")
        return any(origDev.pluginProps.get(k, "") != newDev.pluginProps.get(k, "") for k in keys)

    def _nutrition_targets_changed(self, origDev, newDev):
        return any(origDev.pluginProps.get(k, "") != newDev.pluginProps.get(k, "")
                   for k in self.NUTRITION_TARGETS)

    def _apply_activity_targets(self, dev):
        target_cal = self.resolve_target_value(dev.pluginProps.get("targetCalories", "0"))
        target_work = self.resolve_target_value(dev.pluginProps.get("targetWorkout", "0"))
        target_stand = self.resolve_target_value(dev.pluginProps.get("targetStand", "0"))
        dev.updateStatesOnServer([
            {"key": "day01TargetCalories", "value": target_cal},
            {"key": "day01TargetWorkout", "value": target_work},
            {"key": "day01TargetStand", "value": target_stand},
        ])
        self.calculate_activity_percentages(indigo.devices[dev.id])
        return target_cal, target_work, target_stand

    def deviceUpdated(self, origDev, newDev):
        """Called when ANY device in Indigo is updated. Filter aggressively."""
        if not hasattr(newDev, 'deviceTypeId') or newDev.pluginId != self.pluginId:
            super(Plugin, self).deviceUpdated(origDev, newDev)
            return

        try:
            if newDev.deviceTypeId == "activity" and self._activity_targets_changed(origDev, newDev):
                cal, work, stand = self._apply_activity_targets(newDev)
                self.logger.info(f"Updated targets for {newDev.name}: {cal} cal, {work} min, {stand} hrs")

            elif newDev.deviceTypeId == "nutrition" and self._nutrition_targets_changed(origDev, newDev):
                self.calculate_nutrition_percentages(indigo.devices[newDev.id])
                self.logger.info(f"Updated nutrition targets for {newDev.name}")
        except Exception as e:
            self.logger.error(f"Error in deviceUpdated: {e}")
            self.logger.error(traceback.format_exc())

        super(Plugin, self).deviceUpdated(origDev, newDev)

    def variableUpdated(self, origVariable, newVariable):
        """Called when ANY Indigo variable changes. Only react if a target
        on one of our devices references it."""
        try:
            var_name = newVariable.name

            for dev in indigo.devices.iter("self.activity"):
                refs = (dev.pluginProps.get("targetCalories", ""),
                        dev.pluginProps.get("targetWorkout", ""),
                        dev.pluginProps.get("targetStand", ""))
                if var_name in refs:
                    self._apply_activity_targets(dev)
                    self.logger.info(f"Updated targets for {dev.name} (variable '{var_name}' changed)")

            for dev in indigo.devices.iter("self.nutrition"):
                refs = [dev.pluginProps.get(k, "") for k in self.NUTRITION_TARGETS]
                if var_name in refs:
                    self.calculate_nutrition_percentages(dev)
                    self.logger.info(f"Updated nutrition targets for {dev.name} (variable '{var_name}' changed)")
        except Exception as e:
            self.logger.error(f"Error in variableUpdated: {e}")
            self.logger.error(traceback.format_exc())

        super(Plugin, self).variableUpdated(origVariable, newVariable)

    def deviceStartComm(self, dev):
        """Called when device communication starts"""
        if self.debug:
            self.logger.debug(f"Starting device: {dev.name}")

        # Pick up any new states added to Devices.xml in an upgrade
        dev.stateListOrDisplayStateIdChanged()

        if dev.deviceTypeId == "workouts" and "workoutCount" not in dev.states:
            dev.updateStateOnServer("workoutCount", 0)

        if dev.states.get("lastUpdate", "") in ("", None):
            dev.updateStateOnServer("lastUpdate", "Never")

        if dev.deviceTypeId in self.EVENT_DEVICE_TYPES:
            history = self._load_json_prop(dev, "eventHistory", [])
            self.update_event_states(dev, dev.deviceTypeId, history)

    def deviceStopComm(self, dev):
        """Called when device communication stops"""
        if self.debug:
            self.logger.debug(f"Stopping device: {dev.name}")

    ########################################
    # Menu items
    ########################################

    def logUnmappedMetrics(self):
        if not self.unmapped_metrics:
            self.logger.info("No unmapped metrics received since the plugin started.")
            return
        self.logger.info("Unmapped metrics received since plugin start (name: payload count):")
        for name, count in sorted(self.unmapped_metrics.items()):
            self.logger.info(f"   {name}: {count}")

    def toggleDebugging(self):
        self.debug = not self.debug
        self.pluginPrefs["showDebugInfo"] = self.debug
        self.logger.info(f"Debug logging {'enabled' if self.debug else 'disabled'}")

    ########################################
    # Health data processing (main thread only)
    ########################################

    def process_health_data(self, user_id, data, meta=None):
        """Process incoming health data. Called only from the main thread."""
        meta = meta or {}
        try:
            self.logger.info("=" * 60)
            automation = meta.get("automationName", "")
            suffix = f" (automation: {automation})" if automation else ""
            self.logger.info(f"Processing health data for user: {user_id}{suffix}")

            if self.debug:
                self.logger.debug(f"Data keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
                if any(meta.values()):
                    self.logger.debug(f"Request headers: {meta}")

            # Health Auto Export wraps in {"data": {...}}; older builds didn't.
            payload = data.get("data", data) if isinstance(data, dict) else {}
            handled = False

            if payload.get("workouts"):
                handled = True
                self.logger.info(f"Found {len(payload['workouts'])} workout(s)")
                self.process_workouts(user_id, payload["workouts"])

            if payload.get("metrics"):
                handled = True
                self._dispatch_metrics(user_id, payload["metrics"])

            event_arrays = (
                ("stateOfMind", "stateOfMind", "State of Mind entr(y/ies)"),
                ("ecg", "heartEvents", "ECG recording(s)"),
                ("heartRateNotifications", "heartEvents", "heart rate notification(s)"),
                ("medications", "medications", "medication dose event(s)"),
                ("symptoms", "symptoms", "symptom entr(y/ies)"),
            )
            for key, device_type, label in event_arrays:
                if payload.get(key):
                    handled = True
                    self.logger.info(f"Found {len(payload[key])} {label}")
                    self.process_events(user_id, device_type, key, payload[key])

            if payload.get("cycleTracking"):
                handled = True
                self._note_unmapped("cycleTracking", kind="data type")

            if not handled:
                # An empty export (no new data since last sync) is normal
                known = ("workouts", "metrics", "stateOfMind", "ecg", "heartRateNotifications",
                         "medications", "symptoms", "cycleTracking")
                if isinstance(payload, dict) and any(k in payload for k in known):
                    self.logger.info("Payload contained no new data")
                else:
                    self.logger.warning(
                        f"Unknown data structure: "
                        f"{list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__}"
                    )

            self.logger.info("=" * 60)

        except Exception as e:
            self.logger.error(f"Error processing health data: {str(e)}")
            self.logger.error(traceback.format_exc())

    def _dispatch_metrics(self, user_id, metrics):
        """Route metrics to the appropriate per-device processor."""
        buckets = {}

        for metric in metrics:
            metric_name = metric.get("name", "")
            device_type, state_name = self._lookup_metric(metric_name)
            if not device_type:
                self._note_unmapped(metric_name, metric.get("units", ""))
                continue
            buckets.setdefault(device_type, []).append((metric, state_name))
            if self.debug:
                self.logger.debug(f"  Routing {metric_name} -> {device_type}.{state_name}")

        for device_type, items in buckets.items():
            self.logger.info(f"Found {len(items)} {device_type} metric type(s)")
            if device_type == "activity":
                self.process_activity_metrics(user_id, items)
            elif device_type == "nutrition":
                self.process_nutrition_metrics(user_id, items)
            else:
                self.process_measurement_metrics(user_id, items, device_type)

    ########################################
    # Workouts
    ########################################

    @staticmethod
    def _workout_key(workout):
        wid = workout.get("id")
        if wid:
            return str(wid)
        return f"{workout.get('name', '')}|{workout.get('start', '')}"

    @staticmethod
    def _slim_workout(workout):
        """Drop bulky arrays we never read (routes, per-second series)
        before persisting in pluginProps."""
        slim = copy.deepcopy(workout)
        for key in ("route", "stepCount", "walkingAndRunningDistance", "cyclingCadence",
                    "cyclingDistance", "cyclingPower", "cyclingSpeed", "swimDistance",
                    "swimStroke", "basalEnergy", "heartRateRecovery", "events",
                    "segments", "laps", "activities", "splits", "metadata"):
            slim.pop(key, None)
        # Collapse per-minute series we do read into summaries
        if isinstance(slim.get("activeEnergy"), list):
            total = 0.0
            units = ""
            for sample in slim["activeEnergy"]:
                total += Plugin._num(sample.get("qty"))
                units = sample.get("units", units)
            if not units:
                # Legacy heuristic: unit-less series over 2000 were kJ
                units = "kJ" if total > 2000 else "kcal"
            slim["activeEnergy"] = {"qty": total, "units": units}
        if isinstance(slim.get("heartRateData"), list) and slim["heartRateData"]:
            hr = slim["heartRateData"]
            avgs = [Plugin._num(s.get("Avg", s.get("qty"))) for s in hr]
            mins = [Plugin._num(s.get("Min", s.get("qty"))) for s in hr]
            maxs = [Plugin._num(s.get("Max", s.get("qty"))) for s in hr]
            slim["_hrSummary"] = {
                "min": min(mins) if mins else 0,
                "avg": sum(avgs) / len(avgs) if avgs else 0,
                "max": max(maxs) if maxs else 0,
            }
            slim.pop("heartRateData", None)
        return slim

    def process_workouts(self, user_id, new_workouts):
        """Process workouts with rolling, de-duplicated history"""
        try:
            matching_device = self._find_device("workouts", user_id)
            if not matching_device:
                self.logger.info(f"No Workouts device for user '{user_id}' - ignoring gracefully")
                return

            max_workouts = int(matching_device.pluginProps.get("maxWorkouts", "3"))
            workout_history = self._load_json_prop(matching_device, "workoutHistory", [])

            # Key existing history so re-sent workouts replace rather than duplicate
            by_key = {}
            for workout in workout_history:
                by_key[self._workout_key(workout)] = workout

            added = 0
            for workout in new_workouts:
                key = self._workout_key(workout)
                if key not in by_key:
                    added += 1
                by_key[key] = self._slim_workout(workout)
                if self.debug:
                    self.logger.debug(f"    Workout {key}: {workout.get('name', 'N/A')} @ {str(workout.get('start', ''))[:16]}")

            def sort_key(w):
                dt = self._parse_dt(w.get("start"))
                return dt.timestamp() if dt else 0

            workout_history = sorted(by_key.values(), key=sort_key, reverse=True)[:max_workouts]

            self._save_props(matching_device, workoutHistory=workout_history)
            self.update_workout_states(matching_device, workout_history)

            self.logger.info(
                f"✓ Updated {matching_device.name}: {added} new, "
                f"{len(new_workouts) - added} already known, {len(workout_history)} in history"
            )

        except Exception as e:
            self.logger.error(f"Error processing workouts: {str(e)}")
            self.logger.error(traceback.format_exc())

    def _workout_energy_kcal(self, workout):
        """Active energy in kcal, from whichever field this export version uses."""
        for key in ("activeEnergyBurned", "activeEnergy"):
            value = workout.get(key)
            if isinstance(value, dict) and value.get("qty") is not None:
                return self._energy_to_kcal(self._num(value.get("qty")), value.get("units", "kcal"))
            if isinstance(value, list):  # un-slimmed v2 per-minute series
                total = sum(self._num(s.get("qty")) for s in value)
                units = value[0].get("units", "") if value else ""
                if not units:
                    units = "kJ" if total > 2000 else "kcal"
                return self._energy_to_kcal(total, units)
        return 0

    def _workout_hr(self, workout):
        """(min, avg, max) heart rate from v2 summary, falling back to series."""
        hr = workout.get("heartRate")
        if isinstance(hr, dict) and isinstance(hr.get("avg"), dict):
            return (
                int(round(self._num(hr.get("min", {}).get("qty")))),
                int(round(self._num(hr.get("avg", {}).get("qty")))),
                int(round(self._num(hr.get("max", {}).get("qty")))),
            )
        if isinstance(workout.get("avgHeartRate"), dict):
            return (
                0,
                int(round(self._num(workout["avgHeartRate"].get("qty")))),
                int(round(self._num((workout.get("maxHeartRate") or {}).get("qty")))),
            )
        summary = workout.get("_hrSummary")
        if summary:
            return int(summary["min"]), int(summary["avg"]), int(summary["max"])
        series = workout.get("heartRateData")
        if isinstance(series, list) and series:
            avgs = [self._num(s.get("Avg", s.get("qty"))) for s in series]
            return (int(min(self._num(s.get("Min", s.get("qty"))) for s in series)),
                    int(sum(avgs) / len(avgs)),
                    int(max(self._num(s.get("Max", s.get("qty"))) for s in series)))
        return 0, 0, 0

    WORKOUT_SLOT_DEFAULTS = {
        "name": "", "duration": 0, "startTime": "", "endTime": "", "totalEnergy": "",
        "activeEnergyBurnedCalories": 0, "avgHeartRate": 0, "maxHeartRate": 0,
        "minHeartRate": 0, "intensity": "", "temperature": "", "humidity": "",
        "location": "", "distance": "", "elevationUp": "", "effortScore": 0, "source": "",
    }

    def update_workout_states(self, dev, workout_history):
        """Update device states from workout history. ONE batched IPC call."""
        try:
            updates = [
                {"key": "workoutCount", "value": len(workout_history)},
                {"key": "lastUpdate", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ]

            for i in range(20):
                num_str = f"{i + 1:02d}"
                values = dict(self.WORKOUT_SLOT_DEFAULTS)

                if i < len(workout_history):
                    workout = workout_history[i]
                    total_kcal = self._workout_energy_kcal(workout)
                    min_hr, avg_hr, max_hr = self._workout_hr(workout)

                    temp = workout.get("temperature") or {}
                    temp_str = ""
                    if temp.get("qty") is not None:
                        temp_units = temp.get("units", "degC")
                        temp_str = f"{self._num(temp.get('qty')):.1f}°{'F' if temp_units == 'degF' else 'C'}"

                    humidity = workout.get("humidity") or {}
                    effort = workout.get("effortScore") or workout.get("estimatedEffortScore") or {}
                    source = workout.get("source")
                    if isinstance(source, dict):
                        source = source.get("name", "")

                    values.update({
                        "name": workout.get("name", "Unknown"),
                        "duration": int(self._num(workout.get("duration")) / 60),
                        "startTime": workout.get("start", ""),
                        "endTime": workout.get("end", ""),
                        "totalEnergy": f"{total_kcal:.1f} kcal",
                        "activeEnergyBurnedCalories": int(total_kcal),
                        "avgHeartRate": avg_hr,
                        "maxHeartRate": max_hr,
                        "minHeartRate": min_hr,
                        "intensity": self._qty_str(workout.get("intensity")),
                        "temperature": temp_str,
                        "humidity": f"{self._num(humidity.get('qty')):.0f}%" if humidity.get("qty") is not None else "",
                        "location": workout.get("location", ""),
                        "distance": self._qty_str(workout.get("distance"), 2),
                        "elevationUp": self._qty_str(workout.get("elevationUp"), 0),
                        "effortScore": round(self._num(effort.get("qty")), 1) if isinstance(effort, dict) else 0,
                        "source": source or "",
                    })

                    if self.debug:
                        self.logger.debug(f"  Workout {num_str}: {values['name']}, {values['duration']}min, "
                                          f"{avg_hr}bpm avg, {total_kcal:.0f} kcal")

                for field, value in values.items():
                    updates.append({"key": f"workout{num_str}_{field}", "value": value})

            dev.updateStatesOnServer(updates)

        except Exception as e:
            self.logger.error(f"Error updating workout states: {str(e)}")
            self.logger.error(traceback.format_exc())

    ########################################
    # Daily-total helpers (activity + nutrition)
    ########################################

    def _merge_daily_totals(self, history, items, average_states=frozenset(), units_map=None):
        """Merge metrics into {date: {state: value}}.
        Today's value is replaced (exports grow through the day); past days
        keep the larger of stored/new so a partial resend can't shrink them.
        Averaged states are simply replaced."""
        today = self._local_today()

        for metric, state_name in items:
            if units_map is not None and metric.get("units"):
                units_map[state_name] = metric.get("units")

            per_day = {}
            for point in metric.get("data", []):
                dt = self._parse_dt(point.get("date"))
                if not dt or point.get("qty") is None:
                    continue
                per_day.setdefault(dt.strftime("%Y-%m-%d"), []).append(self._num(point.get("qty")))

            for date_only, values in per_day.items():
                if state_name in average_states:
                    value = sum(values) / len(values)
                else:
                    value = sum(values)

                day = history.setdefault(date_only, {})
                if date_only == today or state_name in average_states or state_name not in day:
                    day[state_name] = value
                else:
                    day[state_name] = max(day[state_name], value)

        sorted_dates = sorted(history.keys(), reverse=True)
        return {date: history[date] for date in sorted_dates[:3]}

    ########################################
    # Activity
    ########################################

    def process_activity_metrics(self, user_id, items):
        """Process activity metrics and aggregate by day"""
        try:
            matching_device = self._find_device("activity", user_id)
            if not matching_device:
                self.logger.info(f"No Activity device for user '{user_id}' - ignoring gracefully")
                return

            activity_history = self._load_json_prop(matching_device, "activityHistory", {})
            units_map = self._load_json_prop(matching_device, "activityUnits", {})

            activity_history = self._merge_daily_totals(
                activity_history, items, self.ACTIVITY_AVERAGE_STATES, units_map)

            self._save_props(matching_device, activityHistory=activity_history, activityUnits=units_map)
            self.update_activity_states(matching_device, activity_history)

            self.logger.info(f"✓ Updated {matching_device.name}: {len(activity_history)} day(s) of activity data")

        except Exception as e:
            self.logger.error(f"Error processing activity metrics: {str(e)}")
            self.logger.error(traceback.format_exc())

    def calculate_activity_percentages(self, dev):
        """Calculate percentage completion for calories, workout, and stand goals."""
        try:
            updates = []
            for day_num in range(1, 4):
                day_str = f"{day_num:02d}"

                actual_cal = dev.states.get(f"day{day_str}ActiveEnergyBurnedCalories", 0)
                actual_work = dev.states.get(f"day{day_str}AppleExerciseTime", 0)
                actual_stand = dev.states.get(f"day{day_str}AppleStandHour", 0)

                target_cal = dev.states.get(f"day{day_str}TargetCalories", 0)
                target_work = dev.states.get(f"day{day_str}TargetWorkout", 0)
                target_stand = dev.states.get(f"day{day_str}TargetStand", 0)

                percent_cal = int(round((actual_cal / target_cal * 100))) if target_cal > 0 else 0
                percent_work = int(round((actual_work / target_work * 100))) if target_work > 0 else 0
                percent_stand = int(round((actual_stand / target_stand * 100))) if target_stand > 0 else 0

                updates.extend([
                    {"key": f"day{day_str}PercentCalories", "value": percent_cal},
                    {"key": f"day{day_str}PercentWorkout", "value": percent_work},
                    {"key": f"day{day_str}PercentStand", "value": percent_stand},
                    {"key": f"day{day_str}PercentCaloriesDisplay", "value": f"{percent_cal}%"},
                    {"key": f"day{day_str}PercentWorkoutDisplay", "value": f"{percent_work}%"},
                    {"key": f"day{day_str}PercentStandDisplay", "value": f"{percent_stand}%"},
                ])

            if updates:
                dev.updateStatesOnServer(updates)

        except Exception as e:
            self.logger.error(f"Error calculating activity percentages: {str(e)}")
            self.logger.error(traceback.format_exc())

    def update_activity_states(self, dev, activity_history):
        """Update device states from activity history."""
        try:
            integer_metrics = {
                "AppleExerciseTime", "AppleMoveTime", "AppleStandHour", "ActiveEnergyBurned",
                "BasalEnergyBurned", "FlightsClimbed", "StepCount",
            }
            units_map = self._load_json_prop(dev, "activityUnits", {})
            sorted_dates = sorted(activity_history.keys(), reverse=True)
            all_states = set(self.METRIC_MAPPING.values())

            updates = []
            for day_num in range(1, 4):
                day_str = f"{day_num:02d}"
                if day_num - 1 < len(sorted_dates):
                    date = sorted_dates[day_num - 1]
                    metrics = activity_history[date]
                else:
                    date, metrics = "", {}

                updates.append({"key": f"day{day_str}Date", "value": date})

                for state_name in all_states:
                    if f"day{day_str}{state_name}" not in dev.states:
                        continue
                    total_qty = metrics.get(state_name, 0)
                    if state_name in integer_metrics:
                        updates.append({"key": f"day{day_str}{state_name}", "value": int(round(total_qty))})
                    else:
                        updates.append({"key": f"day{day_str}{state_name}", "value": round(total_qty, 2)})

                    if state_name in ("ActiveEnergyBurned", "BasalEnergyBurned"):
                        # Exports historically arrived in kJ; honour the units if supplied
                        units = units_map.get(state_name, "kJ")
                        kcal = self._energy_to_kcal(total_qty, units)
                        updates.append({"key": f"day{day_str}{state_name}Calories", "value": int(round(kcal))})

            updates.append({"key": "lastUpdate", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

            target_cal = self.resolve_target_value(dev.pluginProps.get("targetCalories", "0"))
            target_work = self.resolve_target_value(dev.pluginProps.get("targetWorkout", "0"))
            target_stand = self.resolve_target_value(dev.pluginProps.get("targetStand", "0"))
            updates.extend([
                {"key": "day01TargetCalories", "value": target_cal},
                {"key": "day01TargetWorkout", "value": target_work},
                {"key": "day01TargetStand", "value": target_stand},
            ])

            dev.updateStatesOnServer(updates)
            self.calculate_activity_percentages(indigo.devices[dev.id])

        except Exception as e:
            self.logger.error(f"Error updating activity states: {str(e)}")
            self.logger.error(traceback.format_exc())

    ########################################
    # Nutrition
    ########################################

    def process_nutrition_metrics(self, user_id, items):
        try:
            dev = self._find_device("nutrition", user_id)
            if not dev:
                self.logger.info(f"No Nutrition device for user '{user_id}' - ignoring gracefully")
                return

            history = self._load_json_prop(dev, "nutritionHistory", {})
            units_map = self._load_json_prop(dev, "nutritionUnits", {})
            history = self._merge_daily_totals(history, items, frozenset(), units_map)
            self._save_props(dev, nutritionHistory=history, nutritionUnits=units_map)
            self.update_nutrition_states(dev, history)
            self.logger.info(f"✓ Updated {dev.name}: {len(history)} day(s) of nutrition data")
        except Exception as e:
            self.logger.error(f"Error processing nutrition metrics: {str(e)}")
            self.logger.error(traceback.format_exc())

    def update_nutrition_states(self, dev, history):
        try:
            sorted_dates = sorted(history.keys(), reverse=True)
            all_states = sorted(set(self.NUTRITION_METRIC_MAPPING.values()))
            units_map = self._load_json_prop(dev, "nutritionUnits", {})
            updates = []

            for day_num in range(1, 4):
                day_str = f"{day_num:02d}"
                if day_num - 1 < len(sorted_dates):
                    date = sorted_dates[day_num - 1]
                    values = history[date]
                else:
                    date, values = "", {}
                updates.append({"key": f"day{day_str}Date", "value": date})
                for state_name in all_states:
                    updates.append({"key": f"day{day_str}{state_name}",
                                    "value": round(values.get(state_name, 0), 2)})

            units_summary = ", ".join(f"{k} {v}" for k, v in sorted(units_map.items()))
            updates.append({"key": "units", "value": units_summary})
            updates.append({"key": "lastUpdate", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            dev.updateStatesOnServer(updates)
            self.calculate_nutrition_percentages(indigo.devices[dev.id])
        except Exception as e:
            self.logger.error(f"Error updating nutrition states: {str(e)}")
            self.logger.error(traceback.format_exc())

    def calculate_nutrition_percentages(self, dev):
        try:
            updates = []
            for field, state_name in self.NUTRITION_TARGETS.items():
                target = self.resolve_target_value(dev.pluginProps.get(field, "")) \
                    if dev.pluginProps.get(field, "") else 0
                updates.append({"key": f"target{state_name}", "value": target})
                for day_num in range(1, 4):
                    day_str = f"{day_num:02d}"
                    actual = self._num(dev.states.get(f"day{day_str}{state_name}", 0))
                    pct = int(round(actual / target * 100)) if target > 0 else 0
                    updates.append({"key": f"day{day_str}{state_name}Percent", "value": pct})
            dev.updateStatesOnServer(updates)
        except Exception as e:
            self.logger.error(f"Error calculating nutrition percentages: {str(e)}")
            self.logger.error(traceback.format_exc())

    ########################################
    # Measurements (body / heart / respiratory / sleep / vitals / other)
    ########################################

    def _point_values(self, metric_name, state_name, point):
        """Extract {state: value} from one data point, handling the metrics
        whose JSON shape isn't a plain 'qty'."""
        if metric_name == "blood_pressure":
            out = {}
            if point.get("systolic") is not None:
                out["BloodPressureSystolic"] = self._num(point["systolic"])
            if point.get("diastolic") is not None:
                out["BloodPressureDiastolic"] = self._num(point["diastolic"])
            return out

        if metric_name == "heart_rate" and ("Avg" in point or "Min" in point):
            out = {}
            if point.get("Avg") is not None:
                out["HeartRate"] = self._num(point["Avg"])
            if point.get("Min") is not None:
                out["HeartRateMin"] = self._num(point["Min"])
            if point.get("Max") is not None:
                out["HeartRateMax"] = self._num(point["Max"])
            return out

        if point.get("qty") is None:
            return {}
        return {state_name: self._num(point["qty"])}

    def _collect_sleep(self, metric):
        """Turn sleep_analysis data (summarised or raw segments) into
        {night_date: {state: value}}. Night is filed under the wake-up date."""
        nights = {}
        data = metric.get("data", [])

        for point in data:
            if "totalSleep" in point or "core" in point or "inBed" in point:
                # Summarised: one entry per night
                dt = self._parse_dt(point.get("date"))
                if not dt:
                    continue
                night = nights.setdefault(dt.strftime("%Y-%m-%d"), {})
                for field, state in self.SLEEP_STAGE_FIELDS.items():
                    if point.get(field) is not None:
                        night[state] = self._num(point[field])
                for field, state in (("sleepStart", "SleepStart"), ("sleepEnd", "SleepEnd"),
                                     ("inBedStart", "InBedStart"), ("inBedEnd", "InBedEnd")):
                    pdt = self._parse_dt(point.get(field))
                    if pdt:
                        night[state] = pdt.strftime("%H:%M")
                if "TotalSleep" not in night:
                    night["TotalSleep"] = sum(night.get(s, 0) for s in
                                              ("AsleepUnspecified", "CoreSleep", "DeepSleep", "REMSleep"))
            else:
                # Raw segments: file under the date of (start + 12h), so an
                # 11pm segment and a 6am segment land on the same night.
                start = self._parse_dt(point.get("startDate") or point.get("start") or point.get("date"))
                end = self._parse_dt(point.get("endDate") or point.get("end")) or start
                if not start:
                    continue
                night_key = (start + timedelta(hours=12)).strftime("%Y-%m-%d")
                night = nights.setdefault(night_key, {"_start": None, "_end": None})
                state = self.SLEEP_SEGMENT_VALUES.get(point.get("value", ""))
                qty = self._num(point.get("qty"))
                if state:
                    night[state] = night.get(state, 0) + qty
                if state and state not in ("AwakeTime", "InBedTime"):
                    if night["_start"] is None or start < night["_start"]:
                        night["_start"] = start
                    if night["_end"] is None or end > night["_end"]:
                        night["_end"] = end

        for night in nights.values():
            start = night.pop("_start", None)
            end = night.pop("_end", None)
            if start:
                night.setdefault("SleepStart", start.strftime("%H:%M"))
            if end:
                night.setdefault("SleepEnd", end.strftime("%H:%M"))
            if "TotalSleep" not in night:
                night["TotalSleep"] = sum(night.get(s, 0) for s in
                                          ("AsleepUnspecified", "CoreSleep", "DeepSleep", "REMSleep"))
            # Backwards-compatible state (was always 0 before - qty is absent in summaries)
            night["SleepAnalysis"] = night["TotalSleep"]
            night["TotalSleepDisplay"] = self._fmt_hours(night["TotalSleep"])

        return nights

    def _collect_daily(self, metric, state_name):
        """{date: {state: value}} for one metric, aggregated per day."""
        metric_name = metric.get("name", "")
        if metric_name == "sleep_analysis":
            return self._collect_sleep(metric)

        samples = {}  # date -> state -> [(ts, value)]
        for point in metric.get("data", []):
            dt = self._parse_dt(point.get("date"))
            if not dt:
                continue
            for state, value in self._point_values(metric_name, state_name, point).items():
                samples.setdefault(dt.strftime("%Y-%m-%d"), {}).setdefault(state, []).append((dt, value))

        result = {}
        for date_only, states in samples.items():
            for state, readings in states.items():
                mode = self.MEASUREMENT_AGGREGATION.get(state, "latest")
                values = [v for _, v in readings]
                if mode == "sum":
                    value = sum(values)
                elif mode == "mean":
                    value = sum(values) / len(values)
                elif mode == "min":
                    value = min(values)
                elif mode == "max":
                    value = max(values)
                else:
                    value = max(readings, key=lambda r: r[0])[1]
                result.setdefault(date_only, {})[state] = value
        return result

    def process_measurement_metrics(self, user_id, items, device_type):
        """Generic processor for all measurement devices."""
        try:
            matching_device = self._find_device(device_type, user_id)
            if not matching_device:
                self.logger.info(f"No {device_type} device for user '{user_id}' - ignoring gracefully")
                return

            history_key = f"{device_type}History"
            latest_key = f"{device_type}Latest"
            measurement_history = self._load_json_prop(matching_device, history_key, {})
            measurement_latest = self._load_json_prop(matching_device, latest_key, {})
            today = self._local_today()

            for metric, state_name in items:
                daily = self._collect_daily(metric, state_name)
                for date_only, values in daily.items():
                    day = measurement_history.setdefault(date_only, {})
                    for state, value in values.items():
                        # Summed states: keep the larger total for past days
                        if (self.MEASUREMENT_AGGREGATION.get(state) == "sum"
                                and date_only != today and state in day):
                            value = max(day[state], value)
                        day[state] = value

                        latest = measurement_latest.get(state)
                        if latest is None or date_only >= latest.get("date", ""):
                            measurement_latest[state] = {"value": value, "date": date_only}

            sorted_dates = sorted(measurement_history.keys(), reverse=True)
            measurement_history = {date: measurement_history[date] for date in sorted_dates[:3]}

            self._save_props(matching_device, **{history_key: measurement_history,
                                                 latest_key: measurement_latest})
            self.update_measurement_states(matching_device, measurement_history,
                                           measurement_latest, device_type)

            self.logger.info(f"✓ Updated {matching_device.name}: {len(measurement_history)} day(s) of {device_type} data")

        except Exception as e:
            self.logger.error(f"Error processing {device_type} measurements: {str(e)}")
            self.logger.error(traceback.format_exc())

    def _state_value(self, state_name, value):
        if state_name in self.STRING_STATES:
            return "" if value is None else str(value)
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return 0

    def _empty_value(self, state_name):
        return "" if state_name in self.STRING_STATES else 0

    def update_measurement_states(self, dev, measurement_history, measurement_latest, device_type):
        """Update device states from measurement history. One batched IPC call."""
        try:
            fill_gaps = dev.pluginProps.get("fillGaps", True)
            updates = []

            def add(key, value):
                if key in dev.states:
                    updates.append({"key": key, "value": value})

            for state_name, latest_data in measurement_latest.items():
                add(f"latest{state_name}", self._state_value(state_name, latest_data.get("value", 0)))
                add(f"latest{state_name}Date", latest_data.get("date", ""))

            sorted_dates = sorted(measurement_history.keys(), reverse=True)

            for day_num in range(1, 4):
                day_str = f"{day_num:02d}"
                if day_num - 1 < len(sorted_dates):
                    date = sorted_dates[day_num - 1]
                    metrics = measurement_history[date]
                else:
                    date, metrics = "", {}

                add(f"day{day_str}Date", date)

                for state_name, value in metrics.items():
                    add(f"day{day_str}{state_name}", self._state_value(state_name, value))

                for state_name, latest_data in measurement_latest.items():
                    if state_name in metrics:
                        continue
                    if fill_gaps:
                        add(f"day{day_str}{state_name}", self._state_value(state_name, latest_data.get("value", 0)))
                    else:
                        add(f"day{day_str}{state_name}", self._empty_value(state_name))

            # Blank anything with no data at all (never received, or cleared)
            written = {u["key"] for u in updates}
            for key in dev.states:
                if key in written or key.endswith(".ui") or key == "lastUpdate":
                    continue
                if key.startswith("latest") or (key.startswith("day0") and not key.endswith("Date")):
                    base = key[6:] if key.startswith("latest") else key[5:]
                    if base.endswith("Date"):
                        base = base[:-4]
                    updates.append({"key": key, "value": "" if (key.endswith("Date") or base in self.STRING_STATES) else 0})

            updates.append({"key": "lastUpdate", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            dev.updateStatesOnServer(updates)

        except Exception as e:
            self.logger.error(f"Error updating {device_type} measurement states: {str(e)}")
            self.logger.error(traceback.format_exc())

    ########################################
    # Event-style data (State of Mind, ECG, HR notifications,
    # medications, symptoms)
    ########################################

    def _event_from_raw(self, source_key, raw):
        """Normalise a raw event into a small dict we persist. Bulky arrays
        (ECG voltages, HR series) are summarised and discarded."""
        if source_key == "stateOfMind":
            return {
                "kind": "stateOfMind",
                "key": raw.get("id") or f"{raw.get('start')}|{raw.get('kind')}",
                "time": raw.get("start", ""),
                "moodKind": raw.get("kind", ""),
                "valence": self._num(raw.get("valence")),
                "classification": raw.get("valenceClassification", ""),
                "labels": raw.get("labels") or [],
                "associations": raw.get("associations") or [],
            }
        if source_key == "ecg":
            return {
                "kind": "ecg",
                "key": f"ecg|{raw.get('start')}",
                "time": raw.get("start", ""),
                "classification": raw.get("classification", ""),
                "averageHeartRate": self._num(raw.get("averageHeartRate")),
            }
        if source_key == "heartRateNotifications":
            hr = [self._num(s.get("qty")) for s in (raw.get("heartRateData") or [])]
            return {
                "kind": "hrNotification",
                "key": f"hrn|{raw.get('heartNotification')}|{raw.get('start')}",
                "time": raw.get("start", ""),
                "type": raw.get("heartNotification", ""),
                "threshold": self._num(raw.get("threshold")),
                "peak": max(hr) if hr else 0,
            }
        if source_key == "medications":
            return {
                "kind": "medication",
                "key": f"med|{raw.get('displayText')}|{raw.get('scheduledDate')}|{raw.get('start')}",
                "time": raw.get("start", ""),
                "name": raw.get("nickname") or raw.get("displayText", ""),
                "status": raw.get("status", ""),
                "scheduled": raw.get("scheduledDate", ""),
                "dose": f"{raw.get('dosage', '')} {raw.get('units', '')}".strip() if raw.get("dosage") is not None else "",
            }
        if source_key == "symptoms":
            return {
                "kind": "symptom",
                "key": f"sym|{raw.get('name')}|{raw.get('start')}",
                "time": raw.get("start", ""),
                "name": raw.get("name", ""),
                "severity": raw.get("severity", ""),
            }
        return None

    def process_events(self, user_id, device_type, source_key, raw_events):
        try:
            dev = self._find_device(device_type, user_id)
            if not dev:
                self.logger.info(f"No {device_type} device for user '{user_id}' - ignoring gracefully")
                return

            history = self._load_json_prop(dev, "eventHistory", [])
            by_key = {e.get("key"): e for e in history}
            added = 0
            for raw in raw_events:
                event = self._event_from_raw(source_key, raw)
                if not event:
                    continue
                if event["key"] not in by_key:
                    added += 1
                by_key[event["key"]] = event

            cutoff = datetime.now().astimezone() - timedelta(days=self.EVENT_HISTORY_DAYS)

            def ts(e):
                dt = self._parse_dt(e.get("time"))
                return dt.timestamp() if dt else 0

            history = [e for e in by_key.values() if ts(e) >= cutoff.timestamp()]
            history.sort(key=ts, reverse=True)
            history = history[:self.EVENT_HISTORY_MAX]

            self._save_props(dev, eventHistory=history)
            self.update_event_states(dev, device_type, history)
            self.logger.info(f"✓ Updated {dev.name}: {added} new {source_key} event(s)")
        except Exception as e:
            self.logger.error(f"Error processing {source_key}: {str(e)}")
            self.logger.error(traceback.format_exc())

    def update_event_states(self, dev, device_type, history):
        """Write 'last' and 'today' states for an event device."""
        today = self._local_today()

        def local(e):
            return self._parse_dt(e.get("time"))

        def is_today(e):
            dt = local(e)
            return bool(dt) and dt.strftime("%Y-%m-%d") == today

        updates = []

        if device_type == "stateOfMind":
            last = history[0] if history else {}
            daily = next((e for e in history if e.get("moodKind") == "daily_mood"), {})
            todays = [e for e in history if is_today(e)]
            avg = round(sum(e["valence"] for e in todays) / len(todays), 2) if todays else 0
            updates += [
                {"key": "lastValence", "value": round(last.get("valence", 0), 2)},
                {"key": "lastClassification", "value": self._pretty(last.get("classification"))},
                {"key": "lastKind", "value": self._pretty(last.get("moodKind"))},
                {"key": "lastLabels", "value": ", ".join(self._pretty(x) for x in last.get("labels", []))},
                {"key": "lastAssociations", "value": ", ".join(self._pretty(x) for x in last.get("associations", []))},
                {"key": "lastTime", "value": self._fmt_dt(local(last)) if last else ""},
                {"key": "lastDailyMoodValence", "value": round(daily.get("valence", 0), 2)},
                {"key": "lastDailyMoodClassification", "value": self._pretty(daily.get("classification"))},
                {"key": "lastDailyMoodDate", "value": local(daily).strftime("%Y-%m-%d") if daily and local(daily) else ""},
                {"key": "todayEntryCount", "value": len(todays)},
                {"key": "todayAverageValence", "value": avg},
            ]

        elif device_type == "heartEvents":
            ecgs = [e for e in history if e.get("kind") == "ecg"]
            notes = [e for e in history if e.get("kind") == "hrNotification"]
            ecg = ecgs[0] if ecgs else {}
            note = notes[0] if notes else {}
            note_type = self._pretty(str(note.get("type", "")).replace("_notification", ""))
            updates += [
                {"key": "lastEcgClassification", "value": ecg.get("classification", "")},
                {"key": "lastEcgAverageHeartRate", "value": int(ecg.get("averageHeartRate", 0))},
                {"key": "lastEcgTime", "value": self._fmt_dt(local(ecg)) if ecg else ""},
                {"key": "ecgCountToday", "value": sum(1 for e in ecgs if is_today(e))},
                {"key": "lastNotificationType", "value": note_type},
                {"key": "lastNotificationTime", "value": self._fmt_dt(local(note)) if note else ""},
                {"key": "lastNotificationThreshold", "value": int(note.get("threshold", 0))},
                {"key": "lastNotificationPeakHeartRate", "value": int(note.get("peak", 0))},
                {"key": "notificationCountToday", "value": sum(1 for e in notes if is_today(e))},
            ]

        elif device_type == "medications":
            last = history[0] if history else {}
            todays = [e for e in history if is_today(e)]
            updates += [
                {"key": "lastDoseName", "value": last.get("name", "")},
                {"key": "lastDoseStatus", "value": last.get("status", "")},
                {"key": "lastDoseTime", "value": self._fmt_dt(local(last)) if last else ""},
                {"key": "lastDoseAmount", "value": last.get("dose", "")},
                {"key": "todayTaken", "value": sum(1 for e in todays if e.get("status") == "Taken")},
                {"key": "todaySkipped", "value": sum(1 for e in todays if e.get("status") == "Skipped")},
                {"key": "todayLogged", "value": len(todays)},
            ]

        elif device_type == "symptoms":
            present = [e for e in history if e.get("severity") not in ("Not Present", "No Change")]
            last = present[0] if present else {}
            todays = [e for e in present if is_today(e)]
            updates += [
                {"key": "lastSymptomName", "value": last.get("name", "")},
                {"key": "lastSymptomSeverity", "value": last.get("severity", "")},
                {"key": "lastSymptomTime", "value": self._fmt_dt(local(last)) if last else ""},
                {"key": "todaySymptomCount", "value": len(todays)},
                {"key": "todaySymptoms", "value": ", ".join(f"{e['name']} ({e['severity']})" for e in todays)},
            ]

        updates.append({"key": "lastUpdate", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        updates = [u for u in updates if u["key"] in dev.states]
        if updates:
            dev.updateStatesOnServer(updates)

    ########################################
    # Action Callbacks
    ########################################

    def _reset_all_states(self, dev):
        """Zero / blank every state on a device, based on current value types."""
        updates = []
        for key, value in dev.states.items():
            if key.endswith(".ui") or key == "lastUpdate":
                continue
            if isinstance(value, bool):
                updates.append({"key": key, "value": False})
            elif isinstance(value, (int, float)):
                updates.append({"key": key, "value": 0})
            else:
                updates.append({"key": key, "value": ""})
        updates.append({"key": "lastUpdate", "value": "Cleared"})
        dev.updateStatesOnServer(updates)

    def clearAllData(self, action):
        """Action callback to clear all data from a device"""
        try:
            device_id = action.props.get("deviceId")
            if not action.props.get("confirmClear", False):
                self.logger.error("Clear All Data action cancelled - confirmation checkbox not checked")
                return

            dev = indigo.devices[int(device_id)]
            device_type = dev.deviceTypeId
            self.logger.info(f"Clearing all data from device: {dev.name}")

            new_props = dev.pluginProps
            if device_type == "workouts":
                new_props["workoutHistory"] = "[]"
            elif device_type == "activity":
                new_props["activityHistory"] = "{}"
            elif device_type == "nutrition":
                new_props["nutritionHistory"] = "{}"
            elif device_type in self.MEASUREMENT_DEVICE_TYPES:
                new_props[f"{device_type}History"] = "{}"
                new_props[f"{device_type}Latest"] = "{}"
            elif device_type in self.EVENT_DEVICE_TYPES:
                new_props["eventHistory"] = "[]"
            dev.replacePluginPropsOnServer(new_props)

            self._reset_all_states(indigo.devices[dev.id])

            # Restore targets on devices that have them
            dev = indigo.devices[dev.id]
            if device_type == "activity":
                self._apply_activity_targets(dev)
            elif device_type == "nutrition":
                self.calculate_nutrition_percentages(dev)

            self.logger.info(f"✓ Cleared all data from {dev.name}")

        except Exception as e:
            self.logger.error(f"Error clearing all data: {str(e)}")
            self.logger.error(traceback.format_exc())

    def clearDayData(self, action):
        """Action callback to clear data for a specific day"""
        try:
            device_id = action.props.get("deviceId")
            day_to_clear = action.props.get("dayToClear", "day01")
            if not action.props.get("confirmClear", False):
                self.logger.error("Clear Day Data action cancelled - confirmation checkbox not checked")
                return

            dev = indigo.devices[int(device_id)]
            device_type = dev.deviceTypeId
            self.logger.info(f"Clearing {day_to_clear} data from device: {dev.name}")
            day_num = int(day_to_clear[-2:])

            if device_type in ("activity", "nutrition"):
                history_key = f"{device_type}History"
                history = self._load_json_prop(dev, history_key, {})
                sorted_dates = sorted(history.keys(), reverse=True)
                if day_num - 1 >= len(sorted_dates):
                    self.logger.warning(f"No data found for {day_to_clear}")
                    return
                date_to_clear = sorted_dates[day_num - 1]
                del history[date_to_clear]
                self._save_props(dev, **{history_key: history})
                dev = indigo.devices[dev.id]
                if device_type == "activity":
                    self.update_activity_states(dev, history)
                else:
                    self.update_nutrition_states(dev, history)
                self.logger.info(f"✓ Cleared {day_to_clear} ({date_to_clear}) from {dev.name}")

            elif device_type in self.MEASUREMENT_DEVICE_TYPES:
                history_key = f"{device_type}History"
                latest_key = f"{device_type}Latest"
                measurement_history = self._load_json_prop(dev, history_key, {})
                measurement_latest = self._load_json_prop(dev, latest_key, {})

                sorted_dates = sorted(measurement_history.keys(), reverse=True)
                if day_num - 1 >= len(sorted_dates):
                    self.logger.warning(f"No data found for {day_to_clear}")
                    return
                date_to_clear = sorted_dates[day_num - 1]
                del measurement_history[date_to_clear]

                for state_name in list(measurement_latest.keys()):
                    if measurement_latest[state_name]["date"] == date_to_clear:
                        replacement = None
                        for date in sorted(measurement_history.keys(), reverse=True):
                            if state_name in measurement_history[date]:
                                replacement = {"value": measurement_history[date][state_name], "date": date}
                                break
                        if replacement:
                            measurement_latest[state_name] = replacement
                        else:
                            del measurement_latest[state_name]

                self._save_props(dev, **{history_key: measurement_history, latest_key: measurement_latest})
                dev = indigo.devices[dev.id]
                self.update_measurement_states(dev, measurement_history, measurement_latest, device_type)
                self.logger.info(f"✓ Cleared {day_to_clear} ({date_to_clear}) from {dev.name}")

        except Exception as e:
            self.logger.error(f"Error clearing day data: {str(e)}")
            self.logger.error(traceback.format_exc())

    def getDeviceList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Return list of devices for action UI"""
        allowed = None
        if filter:
            allowed = {f.split(".", 1)[-1] for f in filter.split(",") if f.strip()}
        device_list = []
        for dev in indigo.devices.iter("self"):
            if allowed and dev.deviceTypeId not in allowed:
                continue
            device_list.append((dev.id, dev.name))
        return device_list
