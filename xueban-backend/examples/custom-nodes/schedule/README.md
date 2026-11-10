# Schedule Node

The Schedule node enables time-based triggering of workflows with support for cron expressions, intervals, and various scheduling modes.

## Features

- **Multiple Scheduling Modes**: Cron, interval, once, daily, weekly, monthly
- **Timezone Support**: Full timezone support with automatic conversion
- **Time Windows**: Restrict triggers to specific time periods
- **Holiday Skipping**: Skip triggers on configured holidays
- **Trigger Limits**: Maximum number of triggers supported
- **Manual Override**: Force trigger with input override
- **Next Trigger Prediction**: Know when the next trigger will occur

## Configuration

### Scheduling Modes

| Mode | Description | Required Config |
|------|-------------|-----------------|
| `cron` | Standard cron expression | `cron` |
| `interval` | Fixed time interval | `interval` |
| `once` | Single trigger at specific time | `time` |
| `daily` | Trigger every day at specific time | `time` |
| `weekly` | Trigger on specific day at time | `day_of_week`, `time` |
| `monthly` | Trigger on specific day at time | `day_of_month`, `time` |

### Cron Expression Format

```
* * * * *
│ │ │ │ │
│ │ │ │ └── Day of week (0-6, Sunday=0)
│ │ │ └──── Month (1-12)
│ │ └────── Day of month (1-31)
│ └──────── Hour (0-23)
└────────── Minute (0-59)
```

### Configuration Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `mode` | string | cron | Schedule mode |
| `cron` | string | 0 * * * * | Cron expression (cron mode) |
| `interval.value` | number | - | Interval value |
| `interval.unit` | string | minutes | Interval unit (seconds, minutes, hours, days) |
| `time` | string | 00:00 | Time in HH:MM format |
| `day_of_week` | string | monday | Day for weekly mode |
| `day_of_month` | integer | 1 | Day for monthly mode (1-31) |
| `timezone` | string | UTC | Timezone identifier |
| `max_triggers` | integer | 0 | Max triggers (0 = unlimited) |
| `window.start` | string | - | Window start time (HH:MM) |
| `window.end` | string | - | Window end time (HH:MM) |
| `skip_holidays` | boolean | false | Skip on holidays |
| `holidays` | array | [] | Holiday dates (YYYY-MM-DD) |

## Inputs

| Input | Type | Required | Description |
|-------|------|----------|-------------|
| `trigger_override` | boolean | false | Force immediate trigger |
| `schedule_config` | object | false | Override schedule dynamically |

## Outputs

| Output | Type | Description |
|--------|------|-------------|
| `trigger_event` | object | Trigger event with metadata |
| `is_triggered` | boolean | True if triggered this execution |
| `next_trigger` | string | ISO timestamp of next trigger |

## Usage Examples

### Example 1: Cron Schedule (Every Hour)

```yaml
nodes:
  - id: hourly_task
    type: schedule
    config:
      mode: cron
      cron: "0 * * * *"
      timezone: UTC
```

### Example 2: Daily at 9 AM

```yaml
nodes:
  - id: morning_report
    type: schedule
    config:
      mode: daily
      time: "09:00"
      timezone: "America/New_York"
```

### Example 3: Weekly on Monday at 10 AM

```yaml
nodes:
  - id: weekly_meeting
    type: schedule
    config:
      mode: weekly
      day_of_week: monday
      time: "10:00"
      timezone: "Europe/London"
```

### Example 4: Monthly on 1st

```yaml
nodes:
  - id: monthly_invoice
    type: schedule
    config:
      mode: monthly
      day_of_month: 1
      time: "00:00"
      timezone: UTC
```

### Example 5: Every 5 Minutes

```yaml
nodes:
  - id: frequent_check
    type: schedule
    config:
      mode: interval
      interval:
        value: 5
        unit: minutes
```

### Example 6: Business Hours Only (9 AM - 5 PM)

```yaml
nodes:
  - id: business_hours_task
    type: schedule
    config:
      mode: interval
      interval:
        value: 30
        unit: minutes
      timezone: "America/Chicago"
      window:
        start: "09:00"
        end: "17:00"
```

### Example 7: Skip Holidays

```yaml
nodes:
  - id: workday_task
    type: schedule
    config:
      mode: daily
      time: "09:00"
      timezone: "America/New_York"
      skip_holidays: true
      holidays:
        - "2024-01-01"  # New Year's Day
        - "2024-07-04"  # Independence Day
        - "2024-12-25"  # Christmas Day
```

### Example 8: Complex Cron (Weekdays at 8:30 AM)

```yaml
nodes:
  - id: weekday_morning
    type: schedule
    config:
      mode: cron
      cron: "30 8 * * 1-5"
      timezone: UTC
```

### Example 9: Manual Trigger Override

```yaml
nodes:
  - id: manual_trigger_check
    type: code
    outputs:
      should_trigger: true  # Set based on some condition

  - id: scheduled_task
    type: schedule
    config:
      mode: daily
      time: "09:00"
    inputs:
      trigger_override: $manual_trigger_check.should_trigger
```

### Example 10: Limited Number of Triggers

```yaml
nodes:
  - id: limited_schedule
    type: schedule
    config:
      mode: interval
      interval:
        value: 10
        unit: minutes
      max_triggers: 6  # Only trigger 6 times
```

### Example 11: Dynamic Schedule Override

```yaml
nodes:
  - id: calculate_schedule
    type: code
    outputs:
      new_schedule:
        mode: interval
        interval:
          value: 30
          unit: minutes

  - id: dynamic_schedule
    type: schedule
    config:
      mode: cron
      cron: "0 * * * *"
    inputs:
      schedule_config: $calculate_schedule.new_schedule
```

### Example 12: Common Cron Patterns

```yaml
# Every minute
cron: "* * * * *"

# Every 5 minutes
cron: "*/5 * * * *"

# Every hour
cron: "0 * * * *"

# Every day at midnight
cron: "0 0 * * *"

# Every Monday at 9 AM
cron: "0 9 * * 1"

# Weekdays at 9 AM
cron: "0 9 * * 1-5"

# First day of every month at noon
cron: "0 12 1 * *"

# Every 6 hours
cron: "0 */6 * * *"
```

## Trigger Event

When triggered, the `trigger_event` output contains:

```json
{
  "timestamp": "2024-03-07T09:00:00.000Z",
  "reason": "scheduled",
  "timezone": "UTC",
  "trigger_count": 5,
  "mode": "daily",
  "backoff_enabled": false
}
```

### Trigger Reasons

| Reason | Description |
|--------|-------------|
| `scheduled` | Normal scheduled trigger |
| `manual_override` | Triggered via `trigger_override` input |

## Supported Timezones

Common timezone identifiers:

| Region | Timezone |
|--------|----------|
| UTC | `UTC` |
| US Eastern | `America/New_York` |
| US Pacific | `America/Los_Angeles` |
| Europe London | `Europe/London` |
| Europe Paris | `Europe/Paris` |
| Asia Shanghai | `Asia/Shanghai` |
| Asia Tokyo | `Asia/Tokyo` |
| Australia Sydney | `Australia/Sydney` |

## Best Practices

1. **Use Timezones** consistently across your workflow
2. **Set Time Windows** to restrict triggers to business hours
3. **Skip Holidays** for work-related tasks
4. **Limit Triggers** for one-time or limited-run tasks
5. **Use Intervals** for frequent, simple schedules
6. **Use Cron** for complex, recurring schedules
7. **Monitor Next Trigger** to understand when next execution will occur

## Dependencies

This node requires the following packages:

```bash
npm install cron-parser@^4.0.0 date-fns@^3.0.0 date-fns-tz@^2.0.0
```

## License

This custom node is part of AgentGraph and follows the same license.
