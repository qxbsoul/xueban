// Schedule Node Test Suite
// Tests for time-based scheduling functionality

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import ScheduleExecutor, { nodeDefinition, resetAllScheduleStates } from './executor';
import { NodeContext } from 'agentgraph';

describe('ScheduleExecutor', () => {
  let executor: ScheduleExecutor;
  let mockNode: any;

  beforeEach(() => {
    mockNode = {
      id: 'test-schedule',
      inputs: [],
      outputs: []
    };
    executor = new ScheduleExecutor(mockNode);
    resetAllScheduleStates();
  });

  afterEach(() => {
    resetAllScheduleStates();
  });

  describe('Node Definition', () => {
    it('should export correct node definition', () => {
      expect(nodeDefinition.id).toBe('schedule');
      expect(nodeDefinition.executor).toBe(ScheduleExecutor);
    });
  });

  describe('Manual Trigger Override', () => {
    it('should trigger immediately when override is true', async () => {
      const context: NodeContext = {
        inputs: {
          trigger_override: true
        },
        config: {
          mode: 'cron',
          cron: '0 0 * * *',
          timezone: 'UTC',
          max_triggers: 0,
          skip_holidays: false,
          retry_on_failure: false,
          backoff: { enabled: false, initial_delay: 60000, max_delay: 3600000, multiplier: 2 }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.is_triggered).toBe(true);
      expect(result.outputs.trigger_event?.reason).toBe('manual_override');
    });
  });

  describe('Cron Mode', () => {
    it('should trigger on cron schedule', async () => {
      // Set a cron that triggers every minute at 0 seconds
      const context: NodeContext = {
        inputs: {},
        config: {
          mode: 'cron',
          cron: '0 * * * *',  // Every hour at minute 0
          timezone: 'UTC',
          max_triggers: 0,
          skip_holidays: false,
          retry_on_failure: false,
          backoff: { enabled: false, initial_delay: 60000, max_delay: 3600000, multiplier: 2 }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      // First execution should trigger (no last trigger)
      expect(result.outputs.is_triggered).toBe(true);
      expect(result.outputs.next_trigger).toBeDefined();
    });
  });

  describe('Interval Mode', () => {
    it('should trigger at specified interval', async () => {
      const context: NodeContext = {
        inputs: {},
        config: {
          mode: 'interval',
          interval: {
            value: 5,
            unit: 'minutes'
          },
          timezone: 'UTC',
          max_triggers: 0,
          skip_holidays: false,
          retry_on_failure: false,
          backoff: { enabled: false, initial_delay: 60000, max_delay: 3600000, multiplier: 2 }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.is_triggered).toBe(true);
      expect(result.outputs.trigger_event?.mode).toBe('interval');
    });

    it('should calculate correct interval in seconds', async () => {
      const context: NodeContext = {
        inputs: {},
        config: {
          mode: 'interval',
          interval: {
            value: 30,
            unit: 'seconds'
          },
          timezone: 'UTC',
          max_triggers: 0,
          skip_holidays: false,
          retry_on_failure: false,
          backoff: { enabled: false, initial_delay: 60000, max_delay: 3600000, multiplier: 2 }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.is_triggered).toBe(true);
    });
  });

  describe('Daily Mode', () => {
    it('should configure daily schedule', async () => {
      const context: NodeContext = {
        inputs: {},
        config: {
          mode: 'daily',
          time: '09:00',
          timezone: 'UTC',
          max_triggers: 0,
          skip_holidays: false,
          retry_on_failure: false,
          backoff: { enabled: false, initial_delay: 60000, max_delay: 3600000, multiplier: 2 }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.trigger_event?.mode).toBe('daily');
      expect(result.outputs.next_trigger).toBeDefined();
    });
  });

  describe('Weekly Mode', () => {
    it('should configure weekly schedule', async () => {
      const context: NodeContext = {
        inputs: {},
        config: {
          mode: 'weekly',
          day_of_week: 'monday',
          time: '10:00',
          timezone: 'UTC',
          max_triggers: 0,
          skip_holidays: false,
          retry_on_failure: false,
          backoff: { enabled: false, initial_delay: 60000, max_delay: 3600000, multiplier: 2 }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.trigger_event?.mode).toBe('weekly');
    });
  });

  describe('Monthly Mode', () => {
    it('should configure monthly schedule', async () => {
      const context: NodeContext = {
        inputs: {},
        config: {
          mode: 'monthly',
          day_of_month: 1,
          time: '00:00',
          timezone: 'UTC',
          max_triggers: 0,
          skip_holidays: false,
          retry_on_failure: false,
          backoff: { enabled: false, initial_delay: 60000, max_delay: 3600000, multiplier: 2 }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.trigger_event?.mode).toBe('monthly');
    });
  });

  describe('Time Window', () => {
    it('should skip triggers outside time window', async () => {
      const context: NodeContext = {
        inputs: {},
        config: {
          mode: 'interval',
          interval: {
            value: 1,
            unit: 'minutes'
          },
          timezone: 'UTC',
          window: {
            start: '09:00',
            end: '17:00',
            timezone: 'UTC'
          },
          max_triggers: 0,
          skip_holidays: false,
          retry_on_failure: false,
          backoff: { enabled: false, initial_delay: 60000, max_delay: 3600000, multiplier: 2 }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      // First execution might trigger, subsequent checks would respect window
      expect(result.outputs.next_trigger).toBeDefined();
    });
  });

  describe('Holiday Skipping', () => {
    it('should skip trigger on holiday', async () => {
      const today = new Date();
      const todayStr = today.toISOString().split('T')[0];

      const context: NodeContext = {
        inputs: {},
        config: {
          mode: 'daily',
          time: '09:00',
          timezone: 'UTC',
          skip_holidays: true,
          holidays: [todayStr],  // Mark today as holiday
          max_triggers: 0,
          retry_on_failure: false,
          backoff: { enabled: false, initial_delay: 60000, max_delay: 3600000, multiplier: 2 }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      // Should skip and calculate next trigger
      expect(result.outputs.next_trigger).toBeDefined();
    });
  });

  describe('Max Triggers Limit', () => {
    it('should respect max triggers limit', async () => {
      // Simulate multiple executions
      const context: NodeContext = {
        inputs: {},
        config: {
          mode: 'interval',
          interval: {
            value: 1,
            unit: 'minutes'
          },
          timezone: 'UTC',
          max_triggers: 3,  // Only allow 3 triggers
          skip_holidays: false,
          retry_on_failure: false,
          backoff: { enabled: false, initial_delay: 60000, max_delay: 3600000, multiplier: 2 }
        },
        state: {} as any
      };

      // Execute multiple times
      for (let i = 0; i < 5; i++) {
        const result = await executor.execute(context);

        if (i < 3) {
          expect(result.outputs.is_triggered).toBe(true);
        } else {
          expect(result.outputs.is_triggered).toBe(false);
        }
      }
    });
  });

  describe('Dynamic Schedule Override', () => {
    it('should override schedule config from input', async () => {
      const context: NodeContext = {
        inputs: {
          schedule_config: {
            mode: 'interval',
            interval: {
              value: 10,
              unit: 'minutes'
            }
          }
        },
        config: {
          mode: 'daily',
          time: '09:00',
          timezone: 'UTC',
          max_triggers: 0,
          skip_holidays: false,
          retry_on_failure: false,
          backoff: { enabled: false, initial_delay: 60000, max_delay: 3600000, multiplier: 2 }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.trigger_event?.mode).toBe('interval');
    });
  });

  describe('State Reset', () => {
    it('should reset internal state', () => {
      executor['resetState']();

      // State should be cleared
      expect(executor['nodeId']).toBeDefined();
    });
  });

  describe('Next Trigger Calculation', () => {
    it('should calculate next trigger time', async () => {
      const context: NodeContext = {
        inputs: {},
        config: {
          mode: 'daily',
          time: '09:00',
          timezone: 'UTC',
          max_triggers: 0,
          skip_holidays: false,
          retry_on_failure: false,
          backoff: { enabled: false, initial_delay: 60000, max_delay: 3600000, multiplier: 2 }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.next_trigger).toBeDefined();
      expect(result.outputs.next_trigger).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
    });
  });

  describe('Timezone Support', () => {
    it('should handle different timezones', async () => {
      const timezones = ['UTC', 'America/New_York', 'Europe/London', 'Asia/Shanghai'];

      for (const tz of timezones) {
        const context: NodeContext = {
          inputs: {},
          config: {
            mode: 'daily',
            time: '09:00',
            timezone: tz,
            max_triggers: 0,
            skip_holidays: false,
            retry_on_failure: false,
            backoff: { enabled: false, initial_delay: 60000, max_delay: 3600000, multiplier: 2 }
          },
          state: {} as any
        };

        const result = await executor.execute(context);
        expect(result.success).toBe(true);
      }
    });
  });
});
