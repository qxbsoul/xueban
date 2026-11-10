// Filter Node Test Suite
// Tests for data filtering functionality

import { describe, it, expect, beforeEach } from 'vitest';
import FilterExecutor, { nodeDefinition } from './executor';
import { NodeContext } from 'agentgraph';

describe('FilterExecutor', () => {
  let executor: FilterExecutor;
  let mockNode: any;
  const sampleData = [
    { id: 1, name: 'Alice', age: 30, status: 'active', tags: ['admin', 'user'] },
    { id: 2, name: 'Bob', age: 25, status: 'inactive', tags: ['user'] },
    { id: 3, name: 'Charlie', age: 35, status: 'active', tags: ['user'] },
    { id: 4, name: 'Diana', age: 28, status: 'active', tags: ['admin'] }
  ];

  beforeEach(() => {
    mockNode = {
      id: 'filter',
      inputs: [],
      outputs: []
    };
    executor = new FilterExecutor(mockNode);
  });

  describe('Node Definition', () => {
    it('should export correct node definition', () => {
      expect(nodeDefinition.id).toBe('filter');
      expect(nodeDefinition.executor).toBe(FilterExecutor);
    });
  });

  describe('Array Mode', () => {
    it('should filter array with eq operator', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          condition: {
            field: 'status',
            operator: 'eq',
            value: 'active'
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(3);
      expect(result.outputs.total_count).toBe(4);
      expect(result.outputs.filtered_data).toHaveLength(3);
      expect(result.outputs.filtered_data.every((item: any) => item.status === 'active')).toBe(true);
    });

    it('should filter array with gt operator', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          condition: {
            field: 'age',
            operator: 'gt',
            value: 30
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(1);
      expect(result.outputs.filtered_data[0].name).toBe('Charlie');
    });

    it('should filter array with in operator', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          condition: {
            field: 'name',
            operator: 'in',
            value: ['Alice', 'Charlie']
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(2);
      expect(result.outputs.filtered_data.map((item: any) => item.name)).toEqual(['Alice', 'Charlie']);
    });

    it('should filter with contains operator', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          condition: {
            field: 'name',
            operator: 'contains',
            value: 'a'
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(2); // 'Alice' and 'Diana' contain 'a'
    });

    it('should filter with regex operator', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          condition: {
            field: 'name',
            operator: 'regex',
            value: '^A',
            case_sensitive: true
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(1);
      expect(result.outputs.filtered_data[0].name).toBe('Alice');
    });
  });

  describe('Single Mode', () => {
    it('should pass single value matching condition', async () => {
      const context: NodeContext = {
        inputs: { data: { status: 'active', value: 100 } },
        config: {
          mode: 'single',
          condition: {
            field: 'status',
            operator: 'eq',
            value: 'active'
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.passed).toBe(true);
      expect(result.outputs.matched_count).toBe(1);
      expect(result.outputs.filtered_data).toEqual({ status: 'active', value: 100 });
    });

    it('should fail single value not matching condition', async () => {
      const context: NodeContext = {
        inputs: { data: { status: 'inactive', value: 100 } },
        config: {
          mode: 'single',
          condition: {
            field: 'status',
            operator: 'eq',
            value: 'active'
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.passed).toBe(false);
      expect(result.outputs.matched_count).toBe(0);
      expect(result.outputs.filtered_data).toBeNull();
    });
  });

  describe('Object Mode', () => {
    it('should filter object keys', async () => {
      const context: NodeContext = {
        inputs: {
          data: {
            user_name: 'Alice',
            user_email: 'alice@example.com',
            system_config: 'value',
            user_id: '123'
          }
        },
        config: {
          mode: 'object',
          expression: 'key.startsWith("user_")',
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(3);
      expect(Object.keys(result.outputs.filtered_data)).toEqual(['user_name', 'user_email', 'user_id']);
    });
  });

  describe('Custom Expression', () => {
    it('should evaluate custom expression', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          expression: 'item.age >= 30 && item.status === "active"',
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(2); // Alice and Charlie
    });

    it('should use context data in expression', async () => {
      const context: NodeContext = {
        inputs: {
          data: sampleData,
          context: { minAge: 28, allowedStatus: 'active' }
        },
        config: {
          mode: 'array',
          expression: 'item.age >= context.minAge && item.status === context.allowedStatus',
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(2); // Alice and Charlie
    });
  });

  describe('Multiple Conditions', () => {
    it('should evaluate AND conditions', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          conditions: [
            { field: 'status', operator: 'eq', value: 'active', logic: 'AND' },
            { field: 'age', operator: 'gte', value: 30, logic: 'AND' }
          ],
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(2); // Alice and Charlie
    });

    it('should evaluate OR conditions', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          conditions: [
            { field: 'name', operator: 'eq', value: 'Alice', logic: 'OR' },
            { field: 'name', operator: 'eq', value: 'Bob', logic: 'OR' }
          ],
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(2);
    });
  });

  describe('Sorting', () => {
    it('should sort results ascending', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          condition: {
            field: 'status',
            operator: 'eq',
            value: 'active'
          },
          sort: {
            field: 'age',
            order: 'asc'
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      const ages = result.outputs.filtered_data.map((item: any) => item.age);
      expect(ages).toEqual([28, 30, 35]); // Diana, Alice, Charlie
    });

    it('should sort results descending', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          condition: {
            field: 'status',
            operator: 'eq',
            value: 'active'
          },
          sort: {
            field: 'age',
            order: 'desc'
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      const ages = result.outputs.filtered_data.map((item: any) => item.age);
      expect(ages).toEqual([35, 30, 28]); // Charlie, Alice, Diana
    });
  });

  describe('Limit', () => {
    it('should limit results', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          condition: {
            field: 'status',
            operator: 'eq',
            value: 'active'
          },
          limit: 2,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.filtered_data).toHaveLength(2);
      expect(result.outputs.matched_count).toBe(3); // Total matched before limit
    });
  });

  describe('Empty Results', () => {
    it('should return empty array when no matches', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          condition: {
            field: 'status',
            operator: 'eq',
            value: 'nonexistent'
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(0);
      expect(result.outputs.filtered_data).toEqual([]);
      expect(result.outputs.passed).toBe(false);
    });

    it('should return null when no matches and on_empty is return_null', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          condition: {
            field: 'status',
            operator: 'eq',
            value: 'nonexistent'
          },
          limit: 0,
          on_empty: 'return_null'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.filtered_data).toBeNull();
    });

    it('should throw error when no matches and on_empty is throw_error', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          condition: {
            field: 'status',
            operator: 'eq',
            value: 'nonexistent'
          },
          limit: 0,
          on_empty: 'throw_error'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(false);
      expect(result.error?.message).toContain('No items matched');
    });
  });

  describe('Special Operators', () => {
    it('should handle exists operator', async () => {
      const context: NodeContext = {
        inputs: {
          data: [
            { id: 1, name: 'Alice' },
            { id: 2 },  // No name
            { id: 3, name: 'Bob' }
          ]
        },
        config: {
          mode: 'array',
          condition: {
            field: 'name',
            operator: 'exists',
            value: undefined
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(2);
    });

    it('should handle type operator', async () => {
      const context: NodeContext = {
        inputs: {
          data: [
            { id: 1, value: 'string' },
            { id: 2, value: 123 },
            { id: 3, value: true }
          ]
        },
        config: {
          mode: 'array',
          condition: {
            field: 'value',
            operator: 'type',
            value: 'string'
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(1);
    });

    it('should handle truthy operator', async () => {
      const context: NodeContext = {
        inputs: {
          data: [
            { id: 1, active: true },
            { id: 2, active: false },
            { id: 3, active: null }
          ]
        },
        config: {
          mode: 'array',
          condition: {
            field: 'active',
            operator: 'truthy',
            value: undefined
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(1);
    });
  });

  describe('Case Sensitivity', () => {
    it('should perform case-sensitive comparison when enabled', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          condition: {
            field: 'name',
            operator: 'eq',
            value: 'ALICE',
            case_sensitive: true
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(0);
    });

    it('should perform case-insensitive comparison when disabled', async () => {
      const context: NodeContext = {
        inputs: { data: sampleData },
        config: {
          mode: 'array',
          condition: {
            field: 'name',
            operator: 'eq',
            value: 'ALICE',
            case_sensitive: false
          },
          limit: 0,
          on_empty: 'return_empty'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.matched_count).toBe(1);
    });
  });
});
