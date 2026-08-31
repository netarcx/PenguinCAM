# Metrics Collection

UV-CAM collects usage metrics to help understand how the tool is being used. The system is designed to be minimal, fail-safe, and unobtrusive.

## Architecture

- **Database:** SQLite on Railway volume (persists across deployments)
- **Logging:** Asynchronous (fire-and-forget) to avoid slowing down requests
- **Fail-safe:** All DB operations wrapped in try/except - app continues if metrics fail
- **Local dev:** Falls back to `/tmp` if no volume is mounted

## Railway Setup

1. **Create a volume** in your Railway project:
   - Name: `uvcam-metrics` (or any name you prefer)
   - Railway will automatically provide these environment variables:
     - `RAILWAY_VOLUME_NAME` - name of the volume
     - `RAILWAY_VOLUME_MOUNT_PATH` - mount path (e.g., `/data`)

2. **Set admin email** environment variable:
   - Add `ADMIN_EMAIL=your-email@example.com`
   - Only this email can access metrics endpoints

3. **Deploy** - metrics will start collecting automatically

## Events Tracked

| Event Type | When Logged | Metadata |
|------------|-------------|----------|
| `onshape_import` | DXF successfully imported from Onshape | document_name, part_name |
| `gcode_generated` | G-code successfully generated | material, is_tube, from_onshape |
| `download` | User downloads G-code file | filename |
| `drive_save` | G-code saved to Google Drive | filename |

Each event also captures:
- `team_number` - from team config (if available)
- `user_email` - from session (if authenticated)
- `timestamp` - UTC timestamp

## Admin Endpoints

Access metrics via JSON endpoints (requires authentication):

### Summary Stats

```bash
GET /admin/metrics/summary
```

Returns:
```json
{
  "event_counts": {
    "gcode_generated": 150,
    "download": 142,
    "onshape_import": 145,
    "drive_save": 78
  },
  "unique_users": 12,
  "unique_teams": 5,
  "total_events": 515,
  "date_range": {
    "first_event": "2026-01-15T10:30:00",
    "last_event": "2026-02-10T22:15:30"
  }
}
```

### Event Log

```bash
GET /admin/metrics/events?event_type=gcode_generated&limit=100&offset=0
```

Query parameters:
- `event_type` (optional) - filter by event type
- `limit` (optional, default 100, max 1000) - number of events to return
- `offset` (optional, default 0) - pagination offset

Returns:
```json
{
  "events": [
    {
      "id": 123,
      "timestamp": "2026-02-10T22:15:30.123456",
      "event_type": "gcode_generated",
      "team_number": 6238,
      "user_email": "student@team6238.org",
      "metadata": {
        "material": "aluminum",
        "is_tube": false,
        "from_onshape": true
      }
    }
  ],
  "count": 1,
  "limit": 100,
  "offset": 0
}
```

## Authentication

Admin endpoints check that:
1. User is logged in (has `user_email` in session)
2. User's email matches `ADMIN_EMAIL` environment variable

Returns 403 Unauthorized if either check fails.

## Local Development

Metrics work locally without any setup:
- Database created at `/tmp/metrics.db`
- No volume or environment variables needed
- Clean slate on each restart (temp directory cleared)

## Files Added

- `metrics.py` - Core metrics module (~230 lines)
- `frc_cam_gui_app.py` - Added:
  - Import metrics module
  - 4 `metrics.log_event()` calls at key success points
  - 2 admin endpoints with authentication
  - Total: ~60 lines added

## Future Enhancements

If you get addicted to metrics and want more:
- Simple dashboard HTML page (chart.js for visualizations)
- Daily/weekly summary emails
- Real-time usage widget on home page
- Export to CSV for analysis
- More granular events (material choices, machine selections, etc.)
