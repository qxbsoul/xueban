// Data Transform Node Executor
// Transforms data between JSON, XML, and CSV formats with mapping rules

import { parseString, Builder } from 'xml2js';
import { parse as csvParse } from 'csv-parser';
import { createObjectCsvWriter } from 'csv-writer';
import { Readable } from 'stream';
import { BaseNodeExecutor, NodeContext, NodeResult } from 'agentgraph';

interface DataTransformConfig {
  input_format: 'json' | 'xml' | 'csv' | 'auto';
  output_format: 'json' | 'xml' | 'csv';
  mapping: {
    enabled: boolean;
    rules: Array<{
      source: string;
      target: string;
      transform: 'none' | 'upper' | 'lower' | 'trim' | 'date' | 'number' | 'boolean';
    }>;
  };
  json_options: {
    pretty: boolean;
    space: number;
  };
  xml_options: {
    root_element: string;
    item_element: string;
    attributes: boolean;
  };
  csv_options: {
    delimiter: string;
    header: boolean;
    quote: string;
    encoding: string;
  };
  validation: {
    enabled: boolean;
    strict: boolean;
    schema: any;
  };
}

export default class DataTransformExecutor extends BaseNodeExecutor {
  async execute(context: NodeContext): Promise<NodeResult> {
    const { input_data, mapping_rules } = context.inputs;
    const config = context.config as DataTransformConfig;

    try {
      // 1. Validate inputs
      if (!input_data) {
        throw new Error('input_data is required');
      }

      // 2. Detect input format if auto
      const inputFormat = config.input_format === 'auto'
        ? this.detectFormat(input_data)
        : config.input_format;

      // 3. Parse input to internal JSON representation
      const parsedData = await this.parseInput(input_data, inputFormat, config);

      // 4. Apply field mapping if enabled
      const mappedData = config.mapping.enabled
        ? this.applyMapping(parsedData, config.mapping.rules, mapping_rules)
        : parsedData;

      // 5. Validate data if enabled
      if (config.validation.enabled) {
        this.validateData(mappedData, config.validation);
      }

      // 6. Convert to output format
      const outputData = await this.convertOutput(mappedData, config.output_format, config);

      // 7. Create transformation log
      const transformLog = this.createLog({
        inputFormat,
        outputFormat: config.output_format,
        inputSize: this.getDataSize(input_data),
        outputSize: this.getDataSize(outputData),
        mapped: config.mapping.enabled,
        validation: config.validation.enabled
      });

      return this.success({
        output_data: outputData,
        transform_log: transformLog
      });

    } catch (error) {
      return this.failure(error instanceof Error ? error : new Error(String(error)));
    }
  }

  private detectFormat(data: any): 'json' | 'xml' | 'csv' {
    if (typeof data === 'object' && data !== null && !Array.isArray(data)) {
      // Check for XML-like structure (has tagName or xml specific attributes)
      if (data['#name'] || data['$']) {
        return 'xml';
      }
      return 'json';
    }

    if (typeof data === 'string') {
      const trimmed = data.trim();
      if (trimmed.startsWith('<') && trimmed.endsWith('>')) {
        return 'xml';
      }
      if (trimmed.includes(',') || trimmed.includes(';') || trimmed.includes('\t')) {
        return 'csv';
      }
      try {
        JSON.parse(trimmed);
        return 'json';
      } catch {
        // Assume CSV if it looks like tabular data
        return 'csv';
      }
    }

    return 'json';
  }

  private async parseInput(
    data: any,
    format: 'json' | 'xml' | 'csv',
    config: DataTransformConfig
  ): Promise<any> {
    switch (format) {
      case 'json':
        return typeof data === 'string' ? JSON.parse(data) : data;

      case 'xml':
        if (typeof data !== 'string') {
          throw new Error('XML input must be a string');
        }
        return this.parseXML(data);

      case 'csv':
        if (typeof data !== 'string') {
          throw new Error('CSV input must be a string');
        }
        return this.parseCSV(data, config.csv_options);

      default:
        throw new Error(`Unsupported input format: ${format}`);
    }
  }

  private parseXML(xmlString: string): Promise<any> {
    return new Promise((resolve, reject) => {
      parseString(xmlString, { explicitArray: false }, (err, result) => {
        if (err) reject(err);
        else resolve(result);
      });
    });
  }

  private parseCSV(csvString: string, options: DataTransformConfig['csv_options']): Promise<any[]> {
    return new Promise((resolve, reject) => {
      const results: any[] = [];
      const stream = Readable.from([csvString]);

      stream
        .pipe(csvParse({
          separator: options.delimiter === '\\t' ? '\t' : options.delimiter,
          headers: options.header,
          quote: options.quote,
          escape: options.quote,
        }))
        .on('data', (data) => results.push(data))
        .on('end', () => resolve(results))
        .on('error', reject);
    });
  }

  private applyMapping(
    data: any,
    rules: Array<{ source: string; target: string; transform: string }>,
    inputRules?: any[]
  ): any {
    const effectiveRules = inputRules || rules;

    if (!Array.isArray(effectiveRules) || effectiveRules.length === 0) {
      return data;
    }

    const isDataArray = Array.isArray(data);
    const items = isDataArray ? data : [data];

    const mappedItems = items.map((item: any) => {
      const mapped: any = {};

      for (const rule of effectiveRules) {
        const sourceValue = this.getNestedValue(item, rule.source);

        if (sourceValue !== undefined) {
          mapped[rule.target] = this.applyTransform(sourceValue, rule.transform);
        }
      }

      // Include unmapped fields if not strict
      if (Object.keys(mapped).length === 0) {
        return item;
      }

      return mapped;
    });

    return isDataArray ? mappedItems : mappedItems[0];
  }

  private getNestedValue(obj: any, path: string): any {
    return path.split('.').reduce((current, key) => current?.[key], obj);
  }

  private applyTransform(value: any, transform: string): any {
    switch (transform) {
      case 'upper':
        return String(value).toUpperCase();
      case 'lower':
        return String(value).toLowerCase();
      case 'trim':
        return String(value).trim();
      case 'date':
        return new Date(value).toISOString();
      case 'number':
        return Number(value);
      case 'boolean':
        return Boolean(value);
      default:
        return value;
    }
  }

  private validateData(data: any, validation: DataTransformConfig['validation']): void {
    if (!validation.schema) {
      return;
    }

    const items = Array.isArray(data) ? data : [data];

    for (const item of items) {
      for (const [key, type] of Object.entries(validation.schema)) {
        if (validation.strict && !(key in item)) {
          throw new Error(`Validation failed: missing required field '${key}'`);
        }

        if (key in item) {
          const expectedType = String(type);
          const actualType = Array.isArray(item[key]) ? 'array' : typeof item[key];

          if (expectedType !== actualType && expectedType !== 'any') {
            if (validation.strict) {
              throw new Error(
                `Validation failed: field '${key}' expected ${expectedType}, got ${actualType}`
              );
            }
          }
        }
      }
    }
  }

  private async convertOutput(
    data: any,
    format: 'json' | 'xml' | 'csv',
    config: DataTransformConfig
  ): Promise<any> {
    switch (format) {
      case 'json':
        return config.json_options.pretty
          ? JSON.stringify(data, null, config.json_options.space)
          : data;

      case 'xml':
        return this.convertToXML(data, config.xml_options);

      case 'csv':
        return this.convertToCSV(data, config.csv_options);

      default:
        throw new Error(`Unsupported output format: ${format}`);
    }
  }

  private convertToXML(data: any, options: DataTransformConfig['xml_options']): string {
    const builder = new Builder({
      rootName: options.root_element,
      xmldec: { version: '1.0', encoding: 'UTF-8' },
      renderOpts: { pretty: true },
      headless: false
    });

    // Convert array to XML-friendly structure
    let xmlData = data;
    if (Array.isArray(data)) {
      xmlData = { [options.item_element]: data };
    }

    return builder.buildObject(xmlData);
  }

  private convertToCSV(data: any, options: DataTransformConfig['csv_options']): string {
    const items = Array.isArray(data) ? data : [data];

    if (items.length === 0) {
      return '';
    }

    // Get all possible keys from all items
    const headers = [...new Set(items.flatMap(item => Object.keys(item)))];

    let csv = '';

    // Add header row
    if (options.header) {
      csv += headers.map(h => `"${String(h)}"`).join(options.delimiter) + '\n';
    }

    // Add data rows
    for (const item of items) {
      const row = headers.map(header => {
        const value = item[header] ?? '';
        return `"${String(value).replace(/"/g, '""')}"`;
      });
      csv += row.join(options.delimiter) + '\n';
    }

    return csv.trim();
  }

  private getDataSize(data: any): number {
    if (typeof data === 'string') {
      return Buffer.byteLength(data);
    }
    return JSON.stringify(data).length;
  }

  private createLog(metadata: any): any {
    return {
      timestamp: new Date().toISOString(),
      input_format: metadata.inputFormat,
      output_format: metadata.outputFormat,
      input_size: metadata.inputSize,
      output_size: metadata.outputSize,
      size_ratio: (metadata.outputSize / metadata.inputSize).toFixed(2),
      mapping_applied: metadata.mapped,
      validation_applied: metadata.validation,
      status: 'success'
    };
  }
}

// Export node definition for registration
export const nodeDefinition = {
  id: 'data-transform',
  executor: DataTransformExecutor
};
