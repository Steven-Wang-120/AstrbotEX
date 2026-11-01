# Raw ticks and PWM command degrees never masquerade as calibrated radians/metres.
builtin_interfaces/Time published_at
string joint
string backend
string phase
string goal_id
bool control_enabled
bool motion_configured
bool feedback_valid
int32 position_raw
# Wall-clock host receipt, not the sensor sampling time. Zero before first receipt.
builtin_interfaces/Time received_at
float64 receipt_age_ms
float64 query_duration_ms
bool sensor_timestamp_available
bool command_written
int32 target_raw
# NaN when unknown or not applicable.
float64 target_deg
float64 last_written_deg
bool measured_angle_valid
float64 measured_angle_deg
string stop_state
# Always false in v1: STOP packet success is not proof of physical stillness.
bool physical_stop_confirmed
string error
