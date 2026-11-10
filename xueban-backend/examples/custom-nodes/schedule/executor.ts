// Schedule Node Executor
// Triggers workflows based on cron expressions and time-based schedules

import { parseExpression } from 'cron-parser';
import { tz, utcToZonedTime, zonedTimeToUtc } from 'date-fns-tz';
import { format, addSeconds, addMinutes, addHours, addDays, startOfDay, set } from 'date-fns';
import { BaseNodeExecutor, NodeContext, NodeResult } from 'agentgraph';

interface ScheduleConfig {
  mode: 'cron' | 'interval' | 'once' | 'daily' | 'weekly' | 'monthly';
  cron?: string;
  interval?: {
    value: number;
    unit: 'seconds' | 'minutes' | 'hours' | 'days';
  };
  time?: string;
  day_of_week?: string;
  day_of_month?: number;
  timezone: string;
  max_triggers: number;
  window?: {
    start: string;
    end: string;
    timezone?: string;
  };
  skip_holidays: boolean;
  holidays?: string[];
  retry_on_failure: boolean;
  backoff: {
    enabled: boolean;
    initial_delay: number;
    max_delay: number;
    multiplier: number;
  };
}

// Store last trigger time per node instance
const lastTriggerTimes = new Map<string, Date>();
const triggerCounters = new Map<string, number>();

export default class ScheduleExecutor extends BaseNodeExecutor {
  private nodeId: string = '';

  constructor(node: any) {
    super(node);
    this.nodeId = node.id || `schedule-${Date.now()}`;
  }

  async execute(context: NodeContext): Promise<NodeResult> {
    const { trigger_override, schedule_config } = context.inputs;
    const config: ScheduleConfig = { ...context.config, ...schedule_config };

    try {
      // Check for manual trigger override
      if (trigger_override === true) {
        return this.createTriggerResult('manual_override', config);
      }

      // Get current time in configured timezone
      const now = this.getCurrentTime(config.timezone);
      const lastTrigger = lastTriggerTimes.get(this.nodeId);

      // Check if we should trigger now
      const shouldTrigger = this.shouldTrigger(now, lastTrigger, config);

      if (shouldTrigger) {
        // Check trigger count limit
        const triggerCount = triggerCounters.get(this.nodeId) || 0;
        if (config.max_triggers > 0 && triggerCount >= config.max_triggers) {
          return this.createSkippedResult('max_triggers_reached', now, config);
        }

        // Check if it's a holiday
        if (config.skip_holidays && this.isHoliday(now, config.holidays)) {
          return this.createSkippedResult('holiday', now, config);
        }

        // Check time window
        if (!this.isInTimeWindow(now, config)) {
          return this.createSkippedResult('outside_window', now, config);
        }

        // Update last trigger time
        lastTriggerTimes.set(this.nodeId, now);
        triggerCounters.set(this.nodeId, triggerCount + 1);

        return this.createTriggerResult('scheduled', config);
      } else {
        // No trigger, return next trigger time
        const nextTrigger = this.calculateNextTrigger(now, config);
        return this.createPendingResult(now, nextTrigger, config);
      }

    } catch (error) {
      return this.failure(error instanceof Error ? error : new Error(String(error)));
    }
  }

  private getCurrentTime(timezone: string): Date {
    const now = new Date();
    if (timezone === 'UTC') {
      return now;
    }
    return utcToZonedTime(now, timezone);
  }

  private shouldTrigger(now: Date, lastTrigger: Date | undefined, config: ScheduleConfig): boolean {
    // Always trigger if no last trigger
    if (!lastTrigger) {
      return true;
    }

    const diffMs = now.getTime() - lastTrigger.getTime();
    const diffSeconds = diffMs / 1000;

    switch (config.mode) {
      case 'cron':
        return this.checkCronTrigger(now, lastTrigger, config.cron || '');

      case 'interval':
        const intervalMs = this.getIntervalMs(config.interval!);
        return diffMs >= intervalMs;

      case 'once':
        const onceTime = this.parseTime(config.time!, config.timezone);
        const onceDate = set(onceTime, { date: now.getDate(), month: now.getMonth(), year: now.getFullYear() });
        return now >= onceDate && lastTrigger < onceDate;

      case 'daily':
        return this.checkDailyTrigger(now, lastTrigger, config);

      case 'weekly':
        return this.checkWeeklyTrigger(now, lastTrigger, config);

      case 'monthly':
        return this.checkMonthlyTrigger(now, lastTrigger, config);

      default:
        return false;
    }
  }

  private checkCronTrigger(now: Date, lastTrigger: Date, cronExpr: string): boolean {
    try {
      const interval = parseExpression(cronExpr);
      const nextTrigger = interval.next();

      // Check if we're past the scheduled time and haven't triggered yet
      return now >= nextTrigger.toDate() && lastTrigger < nextTrigger.toDate();
    } catch {
      return false;
    }
  }

  private getIntervalMs(interval: { value: number; unit: string }): number {
    const { value, unit } = interval;
    switch (unit) {
      case 'seconds': return value * 1000;
      case 'minutes': return value * 60 * 1000;
      case 'hours': return value * 60 * 60 * 1000;
      case 'days': return value * 24 * 60 * 60 * 1000;
      default: return value * 60 * 1000;
    }
  }

  private checkDailyTrigger(now: Date, lastTrigger: Date, config: ScheduleConfig): boolean {
    const targetTime = this.parseTime(config.time!, config.timezone);
    const todayTarget = set(targetTime, { date: now.getDate(), month: now.getMonth(), year: now.getFullYear() });

    return now >= todayTarget && lastTrigger < todayTarget;
  }

  private checkWeeklyTrigger(now: Date, lastTrigger: Date, config: ScheduleConfig): boolean {
    const targetDay = this.getDayOfWeekNumber(config.day_of_week!);
    const todayDay = now.getDay();

    if (todayDay !== targetDay) {
      return false;
    }

    const targetTime = this.parseTime(config.time!, config.timezone);
    const todayTarget = set(targetTime, { date: now.getDate(), month: now.getMonth(), year: now.getFullYear() });

    return now >= todayTarget && lastTrigger < todayTarget;
  }

  private checkMonthlyTrigger(now: Date, lastTrigger: Date, config: ScheduleConfig): boolean {
    const targetDay = config.day_of_month!;
    const todayDay = now.getDate();

    if (todayDay !== targetDay) {
      return false;
    }

    const targetTime = this.parseTime(config.time!, config.timezone);
    const todayTarget = set(targetTime, { date: now.getDate(), month: now.getMonth(), year: now.getFullYear() });

    return now >= todayTarget && lastTrigger < todayTarget;
  }

  private parseTime(timeStr: string, timezone: string): Date {
    const [hours, minutes] = timeStr.split(':').map(Number);
    const now = new Date();
    const parsed = set(now, { hours, minutes, seconds: 0, milliseconds: 0 });

    if (timezone === 'UTC') {
      return parsed;
    }
    return utcToZonedTime(parsed, timezone);
  }

  private getDayOfWeekNumber(dayName: string): number {
    const days = {
      'sunday': 0,
      'monday': 1,
      'tuesday': 2,
      'wednesday': 3,
      'thursday': 4,
      'friday': 5,
      'saturday': 6
    };
    return days[dayName as keyof typeof days] || 1;
  }

  private isHoliday(date: Date, holidays?: string[]): boolean {
    if (!holidays || holidays.length === 0) {
      return false;
    }

    const dateStr = format(date, 'yyyy-MM-dd');
    return holidays.includes(dateStr);
  }

  private isInTimeWindow(date: Date, config: ScheduleConfig): boolean {
    if (!config.window) {
      return true;
    }

    const windowTimezone = config.window.timezone || config.timezone;
    const zonedDate = utcToZonedTime(date, windowTimezone);
    const hours = zonedDate.getHours();
    const minutes = zonedDate.getMinutes();

    const [startHour, startMin] = config.window.start.split(':').map(Number);
    const [endHour, endMin] = config.window.end.split(':').map(Number);

    const currentMinutes = hours * 60 + minutes;
    const startMinutes = startHour * 60 + startMin;
    const endMinutes = endHour * 60 + endMin;

    return currentMinutes >= startMinutes && currentMinutes <= endMinutes;
  }

  private calculateNextTrigger(now: Date, config: ScheduleConfig): Date | null {
    try {
      switch (config.mode) {
        case 'cron':
          const interval = parseExpression(config.cron || '');
          return interval.next().toDate();

        case 'interval':
          const intervalMs = this.getIntervalMs(config.interval!);
          return addSeconds(now, intervalMs / 1000);

        case 'once':
          const onceTime = this.parseTime(config.time!, config.timezone);
          const onceDate = set(onceTime, { date: now.getDate(), month: now.getMonth(), year: now.getFullYear() });
          return onceDate > now ? onceDate : addDays(onceDate, 1);

        case 'daily':
          const dailyTime = this.parseTime(config.time!, config.timezone);
          const dailyDate = set(dailyTime, { date: now.getDate(), month: now.getMonth(), year: now.getFullYear() });
          return dailyDate > now ? dailyDate : addDays(dailyDate, 1);

        case 'weekly':
          const targetDay = this.getDayOfWeekNumber(config.day_of_week!);
          const weeklyTime = this.parseTime(config.time!, config.timezone);
          let weeklyDate = set(weeklyTime, { date: now.getDate(), month: now.getMonth(), year: now.getFullYear() });

          while (weeklyDate.getDay() !== targetDay || weeklyDate <= now) {
            weeklyDate = addDays(weeklyDate, 1);
          }
          return weeklyDate;

        case 'monthly':
          const targetMonthDay = config.day_of_month!;
          const monthlyTime = this.parseTime(config.time!, config.timezone);
          let monthlyDate = set(monthlyTime, { date: targetMonthDay, month: now.getMonth(), year: now.getFullYear() });

          if (monthlyDate <= now) {
            monthlyDate = addDays(monthlyDate, 32); // Move to next month
            monthlyDate = set(monthlyDate, { date: targetMonthDay });
          }
          return monthlyDate;

        default:
          return null;
      }
    } catch {
      return null;
    }
  }

  private createTriggerResult(reason: string, config: ScheduleConfig): NodeResult {
    const now = new Date();
    const nextTrigger = this.calculateNextTrigger(now, config);

    return this.success({
      trigger_event: {
        timestamp: now.toISOString(),
        reason,
        timezone: config.timezone,
        trigger_count: triggerCounters.get(this.nodeId) || 0,
        mode: config.mode,
        backoff_enabled: config.backoff?.enabled || false
      },
      is_triggered: true,
      next_trigger: nextTrigger?.toISOString()
    });
  }

  private createPendingResult(now: Date, nextTrigger: Date | null, config: ScheduleConfig): NodeResult {
    return this.success({
      trigger_event: null,
      is_triggered: false,
      next_trigger: nextTrigger?.toISOString()
    });
  }

  private createSkippedResult(reason: string, now: Date, config: ScheduleConfig): NodeResult {
    const nextTrigger = this.calculateNextTrigger(addMinutes(now, 1), config);

    return this.success({
      trigger_event: null,
      is_triggered: false,
      next_trigger: nextTrigger?.toISOString()
    });
  }

  // Reset trigger state (useful for testing)
  public resetState(): void {
    lastTriggerTimes.delete(this.nodeId);
    triggerCounters.delete(this.nodeId);
  }
}

// Export node definition for registration
export const nodeDefinition = {
  id: 'schedule',
  executor: ScheduleExecutor
};

// Export helper functions for external use
export function resetAllScheduleStates(): void {
  lastTriggerTimes.clear();
  triggerCounters.clear();
}
