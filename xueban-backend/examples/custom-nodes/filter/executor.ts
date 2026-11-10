// Filter Node Executor
// Filters data based on configurable conditions and expressions

import { get } from 'lodash';
import { JSONPath } from 'jsonpath-plus';
import { BaseNodeExecutor, NodeContext, NodeResult } from 'agentgraph';

interface FilterCondition {
  field: string;
  operator: 'eq' | 'ne' | 'gt' | 'gte' | 'lt' | 'lte' | 'in' | 'not_in' | 'contains' | 'starts_with' | 'ends_with' | 'regex' | 'exists' | 'type' | 'empty' | 'truthy';
  value?: any;
  case_sensitive?: boolean;
}

interface FilterConfig {
  mode: 'array' | 'single' | 'object';
  condition?: FilterCondition;
  expression?: string;
  conditions?: Array<{
    field?: string;
    operator?: string;
    value?: any;
    logic?: 'AND' | 'OR';
  }>;
  limit: number;
  sort?: {
    field: string;
    order: 'asc' | 'desc';
  };
  on_empty: 'return_empty' | 'return_null' | 'throw_error';
}

export default class FilterExecutor extends BaseNodeExecutor {
  async execute(context: NodeContext): Promise<NodeResult> {
    const { data, context: contextData } = context.inputs;
    const config = context.config as FilterConfig;

    try {
      // 1. Validate inputs
      if (data === undefined || data === null) {
        throw new Error('Input data is required');
      }

      // 2. Prepare items based on mode
      let items: any[];
      let isObject = false;

      switch (config.mode) {
        case 'array':
          items = Array.isArray(data) ? data : [data];
          break;
        case 'single':
          const passes = this.evaluateSingle(data, config, contextData);
          return this.success({
            filtered_data: passes ? data : null,
            matched_count: passes ? 1 : 0,
            total_count: 1,
            passed: passes
          });
        case 'object':
          if (typeof data !== 'object' || data === null) {
            throw new Error('Object mode requires object input');
          }
          items = Object.entries(data).map(([key, value]) => ({ key, value }));
          isObject = true;
          break;
        default:
          throw new Error(`Unknown filter mode: ${config.mode}`);
      }

      // 3. Filter items
      const filtered: any[] = [];
      for (const item of items) {
        if (this.evaluateItem(item, config, contextData)) {
          if (isObject) {
            filtered.push({ key: item.key, value: item.value });
          } else {
            filtered.push(item);
          }
        }
      }

      // 4. Apply limit
      let results = filtered;
      if (config.limit > 0) {
        results = filtered.slice(0, config.limit);
      }

      // 5. Sort if configured
      if (config.sort && results.length > 0) {
        results = this.sortResults(results, config.sort);
      }

      // 6. Convert back to original format if object mode
      let outputData: any;
      if (isObject) {
        outputData = {};
        for (const item of results) {
          outputData[item.key] = item.value;
        }
      } else {
        outputData = results;
      }

      // 7. Handle empty results
      const passed = results.length > 0;
      if (!passed) {
        switch (config.on_empty) {
          case 'return_null':
            outputData = null;
            break;
          case 'throw_error':
            throw new Error('No items matched the filter criteria');
          case 'return_empty':
          default:
            break;
        }
      }

      return this.success({
        filtered_data: outputData,
        matched_count: filtered.length,
        total_count: items.length,
        passed
      });

    } catch (error) {
      return this.failure(error instanceof Error ? error : new Error(String(error)));
    }
  }

  private evaluateSingle(data: any, config: FilterConfig, contextData?: any): boolean {
    if (config.expression) {
      return this.evaluateExpression(data, config.expression, contextData);
    }
    if (config.condition) {
      return this.evaluateCondition(data, config.condition, contextData);
    }
    return true;
  }

  private evaluateItem(item: any, config: FilterConfig, contextData?: any): boolean {
    // Use custom expression if provided
    if (config.expression) {
      return this.evaluateExpression(item, config.expression, contextData);
    }

    // Use simple condition if provided
    if (config.condition) {
      return this.evaluateCondition(item, config.condition, contextData);
    }

    // Use multiple conditions if provided
    if (config.conditions && config.conditions.length > 0) {
      return this.evaluateMultipleConditions(item, config.conditions, contextData);
    }

    // Default: pass all items
    return true;
  }

  private evaluateExpression(item: any, expression: string, contextData?: any): boolean {
    try {
      // Build evaluation context
      const evalContext = {
        item,
        $: item,
        idx: -1,
        context: contextData || {},
        _,
        Math,
        Date,
        String,
        Number,
        Boolean,
        Array,
        Object
      };

      // Create safe evaluation function
      const safeEval = new Function('ctx', `
        with (ctx) {
          try {
            return !!(${expression});
          } catch (e) {
            return false;
          }
        }
      `);

      return safeEval(evalContext);
    } catch (error) {
      return false;
    }
  }

  private evaluateCondition(item: any, condition: FilterCondition, contextData?: any): boolean {
    // Get field value using JSONPath for nested access
    let fieldValue: any;
    if (condition.field) {
      fieldValue = JSONPath({ path: condition.field, json: item, wrap: false });
    } else {
      fieldValue = item;
    }

    const compareValue = condition.value;
    const caseSensitive = condition.case_sensitive ?? false;

    switch (condition.operator) {
      case 'eq':
        return this.compareEq(fieldValue, compareValue, caseSensitive);

      case 'ne':
        return !this.compareEq(fieldValue, compareValue, caseSensitive);

      case 'gt':
        return this.toNumber(fieldValue) > this.toNumber(compareValue);

      case 'gte':
        return this.toNumber(fieldValue) >= this.toNumber(compareValue);

      case 'lt':
        return this.toNumber(fieldValue) < this.toNumber(compareValue);

      case 'lte':
        return this.toNumber(fieldValue) <= this.toNumber(compareValue);

      case 'in':
        return Array.isArray(compareValue) && compareValue.includes(fieldValue);

      case 'not_in':
        return Array.isArray(compareValue) && !compareValue.includes(fieldValue);

      case 'contains':
        return this.compareContains(fieldValue, compareValue, caseSensitive);

      case 'starts_with':
        return this.compareStartsWith(fieldValue, compareValue, caseSensitive);

      case 'ends_with':
        return this.compareEndsWith(fieldValue, compareValue, caseSensitive);

      case 'regex':
        return this.compareRegex(fieldValue, compareValue, caseSensitive);

      case 'exists':
        return fieldValue !== undefined && fieldValue !== null;

      case 'type':
        return typeof fieldValue === compareValue;

      case 'empty':
        if (Array.isArray(fieldValue)) return fieldValue.length === 0;
        if (typeof fieldValue === 'string') return fieldValue.length === 0;
        if (typeof fieldValue === 'object' && fieldValue !== null) return Object.keys(fieldValue).length === 0;
        return false;

      case 'truthy':
        return Boolean(fieldValue);

      default:
        return false;
    }
  }

  private evaluateMultipleConditions(
    item: any,
    conditions: Array<{
      field?: string;
      operator?: string;
      value?: any;
      logic?: 'AND' | 'OR';
    }>,
    contextData?: any
  ): boolean {
    let result = true;
    let currentLogic: 'AND' | 'OR' = 'AND';

    for (let i = 0; i < conditions.length; i++) {
      const cond = conditions[i];
      const conditionResult = this.evaluateCondition(item, {
        field: cond.field,
        operator: cond.operator as any,
        value: cond.value,
        case_sensitive: false
      }, contextData);

      // Update logic for next iteration if specified
      if (cond.logic) {
        currentLogic = cond.logic;
      }

      if (currentLogic === 'AND') {
        result = result && conditionResult;
      } else {
        result = result || conditionResult;
      }
    }

    return result;
  }

  private compareEq(a: any, b: any, caseSensitive: boolean): boolean {
    if (typeof a === 'string' && typeof b === 'string' && !caseSensitive) {
      return a.toLowerCase() === b.toLowerCase();
    }
    return a === b;
  }

  private compareContains(haystack: any, needle: any, caseSensitive: boolean): boolean {
    const haystackStr = String(haystack);
    const needleStr = String(needle);
    if (!caseSensitive) {
      return haystackStr.toLowerCase().includes(needleStr.toLowerCase());
    }
    return haystackStr.includes(needleStr);
  }

  private compareStartsWith(str: any, prefix: any, caseSensitive: boolean): boolean {
    const strVal = String(str);
    const prefixVal = String(prefix);
    if (!caseSensitive) {
      return strVal.toLowerCase().startsWith(prefixVal.toLowerCase());
    }
    return strVal.startsWith(prefixVal);
  }

  private compareEndsWith(str: any, suffix: any, caseSensitive: boolean): boolean {
    const strVal = String(str);
    const suffixVal = String(suffix);
    if (!caseSensitive) {
      return strVal.toLowerCase().endsWith(suffixVal.toLowerCase());
    }
    return strVal.endsWith(suffixVal);
  }

  private compareRegex(str: any, pattern: any, caseSensitive: boolean): boolean {
    try {
      const flags = caseSensitive ? 'g' : 'gi';
      const regex = new RegExp(pattern, flags);
      return regex.test(String(str));
    } catch {
      return false;
    }
  }

  private toNumber(value: any): number {
    const num = Number(value);
    return isNaN(num) ? 0 : num;
  }

  private sortResults(results: any[], sortConfig: FilterConfig['sort']): any[] {
    if (!sortConfig) return results;

    return [...results].sort((a, b) => {
      const aVal = get(a, sortConfig!.field);
      const bVal = get(b, sortConfig!.field);

      if (sortConfig!.order === 'asc') {
        return aVal < bVal ? -1 : aVal > bVal ? 1 : 0;
      } else {
        return aVal > bVal ? -1 : aVal < bVal ? 1 : 0;
      }
    });
  }
}

// Export node definition for registration
export const nodeDefinition = {
  id: 'filter',
  executor: FilterExecutor
};
