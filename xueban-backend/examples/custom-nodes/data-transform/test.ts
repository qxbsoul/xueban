// Data Transform Node Test Suite
// Tests for data transformation between JSON, XML, and CSV

import { describe, it, expect, beforeEach } from 'vitest';
import DataTransformExecutor, { nodeDefinition } from './executor';
import { NodeContext } from 'agentgraph';

describe('DataTransformExecutor', () => {
  let executor: DataTransformExecutor;
  let mockNode: any;

  beforeEach(() => {
    mockNode = {
      id: 'data-transform',
      inputs: [],
      outputs: []
    };
    executor = new DataTransformExecutor(mockNode);
  });

  describe('Node Definition', () => {
    it('should export correct node definition', () => {
      expect(nodeDefinition.id).toBe('data-transform');
      expect(nodeDefinition.executor).toBe(DataTransformExecutor);
    });
  });

  describe('Format Detection', () => {
    it('should detect JSON format', () => {
      const json = '{"name": "test"}';
      const format = executor['detectFormat'](json);
      expect(format).toBe('json');
    });

    it('should detect XML format', () => {
      const xml = '<root><name>test</name></root>';
      const format = executor['detectFormat'](xml);
      expect(format).toBe('xml');
    });

    it('should detect CSV format', () => {
      const csv = 'name,age\nAlice,30';
      const format = executor['detectFormat'](csv);
      expect(format).toBe('csv');
    });

    it('should detect object as JSON', () => {
      const obj = { name: 'test' };
      const format = executor['detectFormat'](obj);
      expect(format).toBe('json');
    });
  });

  describe('JSON to JSON', () => {
    it('should pass through JSON data unchanged', async () => {
      const context: NodeContext = {
        inputs: {
          input_data: { name: 'Alice', age: 30 }
        },
        config: {
          input_format: 'json',
          output_format: 'json',
          mapping: { enabled: false, rules: [] },
          json_options: { pretty: false, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: { enabled: false, strict: false, schema: {} }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.output_data).toEqual({ name: 'Alice', age: 30 });
    });

    it('should pretty print JSON when enabled', async () => {
      const context: NodeContext = {
        inputs: {
          input_data: { name: 'Alice' }
        },
        config: {
          input_format: 'json',
          output_format: 'json',
          mapping: { enabled: false, rules: [] },
          json_options: { pretty: true, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: { enabled: false, strict: false, schema: {} }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(typeof result.outputs.output_data).toBe('string');
      expect(result.outputs.output_data).toContain('  ');
    });
  });

  describe('JSON to CSV', () => {
    it('should convert JSON array to CSV', async () => {
      const context: NodeContext = {
        inputs: {
          input_data: [
            { name: 'Alice', age: 30, city: 'NYC' },
            { name: 'Bob', age: 25, city: 'LA' }
          ]
        },
        config: {
          input_format: 'json',
          output_format: 'csv',
          mapping: { enabled: false, rules: [] },
          json_options: { pretty: false, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: { enabled: false, strict: false, schema: {} }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      const csv = result.outputs.output_data;
      expect(csv).toContain('"name"');
      expect(csv).toContain('"Alice"');
      expect(csv).toContain('"Bob"');
    });
  });

  describe('XML to JSON', () => {
    it('should convert XML to JSON', async () => {
      const xml = '<root><user><name>Alice</name><age>30</age></user></root>';
      const context: NodeContext = {
        inputs: {
          input_data: xml
        },
        config: {
          input_format: 'xml',
          output_format: 'json',
          mapping: { enabled: false, rules: [] },
          json_options: { pretty: true, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: { enabled: false, strict: false, schema: {} }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      const json = JSON.parse(result.outputs.output_data);
      expect(json.root.user.name).toBe('Alice');
      expect(json.root.user.age).toBe('30');
    });
  });

  describe('CSV to JSON', () => {
    it('should convert CSV to JSON array', async () => {
      const csv = 'name,age,city\nAlice,30,NYC\nBob,25,LA';
      const context: NodeContext = {
        inputs: {
          input_data: csv
        },
        config: {
          input_format: 'csv',
          output_format: 'json',
          mapping: { enabled: false, rules: [] },
          json_options: { pretty: true, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: { enabled: false, strict: false, schema: {} }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      const data = result.outputs.output_data;
      expect(Array.isArray(data)).toBe(true);
      expect(data).toHaveLength(2);
      expect(data[0]).toEqual({ name: 'Alice', age: '30', city: 'NYC' });
    });
  });

  describe('Field Mapping', () => {
    it('should apply field mapping rules', async () => {
      const context: NodeContext = {
        inputs: {
          input_data: { firstName: 'John', lastName: 'Doe', age: 30 }
        },
        config: {
          input_format: 'json',
          output_format: 'json',
          mapping: {
            enabled: true,
            rules: [
              { source: 'firstName', target: 'first_name', transform: 'lower' },
              { source: 'lastName', target: 'last_name', transform: 'lower' },
              { source: 'age', target: 'user_age', transform: 'number' }
            ]
          },
          json_options: { pretty: false, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: { enabled: false, strict: false, schema: {} }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      const output = result.outputs.output_data;
      expect(output).toEqual({
        first_name: 'john',
        last_name: 'doe',
        user_age: 30
      });
    });

    it('should use mapping_rules from input', async () => {
      const context: NodeContext = {
        inputs: {
          input_data: { oldName: 'Test', oldValue: '100' },
          mapping_rules: [
            { source: 'oldName', target: 'newName', transform: 'upper' },
            { source: 'oldValue', target: 'newValue', transform: 'number' }
          ]
        },
        config: {
          input_format: 'json',
          output_format: 'json',
          mapping: {
            enabled: true,
            rules: []  // Empty, should use input mapping_rules
          },
          json_options: { pretty: false, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: { enabled: false, strict: false, schema: {} }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      const output = result.outputs.output_data;
      expect(output).toEqual({
        newName: 'TEST',
        newValue: 100
      });
    });

    it('should handle nested field paths', async () => {
      const context: NodeContext = {
        inputs: {
          input_data: {
            user: { profile: { name: 'Alice' } }
          }
        },
        config: {
          input_format: 'json',
          output_format: 'json',
          mapping: {
            enabled: true,
            rules: [
              { source: 'user.profile.name', target: 'fullName', transform: 'none' }
            ]
          },
          json_options: { pretty: false, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: { enabled: false, strict: false, schema: {} }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.output_data).toEqual({ fullName: 'Alice' });
    });
  });

  describe('Transform Types', () => {
    it('should apply upper transform', async () => {
      const context: NodeContext = {
        inputs: {
          input_data: { name: 'alice' }
        },
        config: {
          input_format: 'json',
          output_format: 'json',
          mapping: {
            enabled: true,
            rules: [{ source: 'name', target: 'name', transform: 'upper' }]
          },
          json_options: { pretty: false, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: { enabled: false, strict: false, schema: {} }
        },
        state: {} as any
      };

      const result = await executor.execute(context);
      expect(result.outputs.output_data.name).toBe('ALICE');
    });

    it('should apply lower transform', async () => {
      const context: NodeContext = {
        inputs: {
          input_data: { name: 'ALICE' }
        },
        config: {
          input_format: 'json',
          output_format: 'json',
          mapping: {
            enabled: true,
            rules: [{ source: 'name', target: 'name', transform: 'lower' }]
          },
          json_options: { pretty: false, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: { enabled: false, strict: false, schema: {} }
        },
        state: {} as any
      };

      const result = await executor.execute(context);
      expect(result.outputs.output_data.name).toBe('alice');
    });

    it('should apply trim transform', async () => {
      const context: NodeContext = {
        inputs: {
          input_data: { name: '  alice  ' }
        },
        config: {
          input_format: 'json',
          output_format: 'json',
          mapping: {
            enabled: true,
            rules: [{ source: 'name', target: 'name', transform: 'trim' }]
          },
          json_options: { pretty: false, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: { enabled: false, strict: false, schema: {} }
        },
        state: {} as any
      };

      const result = await executor.execute(context);
      expect(result.outputs.output_data.name).toBe('alice');
    });
  });

  describe('Validation', () => {
    it('should validate data against schema', async () => {
      const context: NodeContext = {
        inputs: {
          input_data: { name: 'Alice', age: 30 }
        },
        config: {
          input_format: 'json',
          output_format: 'json',
          mapping: { enabled: false, rules: [] },
          json_options: { pretty: false, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: {
            enabled: true,
            strict: false,
            schema: { name: 'string', age: 'number' }
          }
        },
        state: {} as any
      };

      const result = await executor.execute(context);
      expect(result.success).toBe(true);
    });
  });

  describe('Transform Log', () => {
    it('should include transformation statistics', async () => {
      const context: NodeContext = {
        inputs: {
          input_data: { name: 'Alice' }
        },
        config: {
          input_format: 'json',
          output_format: 'json',
          mapping: { enabled: false, rules: [] },
          json_options: { pretty: false, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: { enabled: false, strict: false, schema: {} }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.outputs.transform_log).toBeDefined();
      expect(result.outputs.transform_log.input_format).toBe('json');
      expect(result.outputs.transform_log.output_format).toBe('json');
      expect(result.outputs.transform_log.status).toBe('success');
      expect(result.outputs.transform_log.timestamp).toBeDefined();
    });
  });

  describe('Error Handling', () => {
    it('should fail when input_data is missing', async () => {
      const context: NodeContext = {
        inputs: {},
        config: {
          input_format: 'json',
          output_format: 'json',
          mapping: { enabled: false, rules: [] },
          json_options: { pretty: false, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: { enabled: false, strict: false, schema: {} }
        },
        state: {} as any
      };

      const result = await executor.execute(context);
      expect(result.success).toBe(false);
      expect(result.error?.message).toContain('input_data');
    });

    it('should fail on invalid JSON', async () => {
      const context: NodeContext = {
        inputs: {
          input_data: '{invalid json}'
        },
        config: {
          input_format: 'json',
          output_format: 'json',
          mapping: { enabled: false, rules: [] },
          json_options: { pretty: false, space: 2 },
          xml_options: { root_element: 'root', item_element: 'item', attributes: true },
          csv_options: { delimiter: ',', header: true, quote: '"', encoding: 'utf-8' },
          validation: { enabled: false, strict: false, schema: {} }
        },
        state: {} as any
      };

      const result = await executor.execute(context);
      expect(result.success).toBe(false);
    });
  });
});
