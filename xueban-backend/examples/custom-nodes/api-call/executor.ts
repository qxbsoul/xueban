// API Call Node Executor
// Executes REST API calls with configurable methods, headers, and body

import axios, { AxiosRequestConfig, AxiosResponse } from 'axios';
import { BaseNodeExecutor, NodeContext, NodeResult } from 'agentgraph';

interface ApiCallConfig {
  url: string;
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE' | 'HEAD' | 'OPTIONS';
  headers: Record<string, string>;
  timeout: number;
  retry: {
    enabled: boolean;
    max_attempts: number;
    delay: number;
  };
  auth: {
    type: 'none' | 'bearer' | 'basic' | 'api_key';
    token?: string;
    username?: string;
    password?: string;
    api_key?: string;
    header_name?: string;
  };
  response_handling: {
    parse_json: boolean;
    ignore_ssl_errors: boolean;
    follow_redirects: boolean;
  };
}

export default class ApiCallExecutor extends BaseNodeExecutor {
  async execute(context: NodeContext): Promise<NodeResult> {
    const { data, headers_override } = context.inputs;
    const config = context.config as ApiCallConfig;

    try {
      // 1. Validate inputs
      this.validateInputs(config, ['url', 'method']);

      // 2. Build request config
      const requestConfig = this.buildRequestConfig(config, data, headers_override);

      // 3. Execute request with retry logic
      const response = await this.executeWithRetry(requestConfig, config.retry);

      // 4. Process response
      const result = this.processResponse(response, config.response_handling);

      return this.success(result);

    } catch (error) {
      return this.failure(this.handleError(error));
    }
  }

  private validateInputs(config: any, required: string[]): void {
    for (const field of required) {
      if (!config[field]) {
        throw new Error(`Missing required config: ${field}`);
      }
    }
  }

  private buildRequestConfig(
    config: ApiCallConfig,
    data?: any,
    headersOverride?: Record<string, string>
  ): AxiosRequestConfig {
    const headers = { ...config.headers };

    // Apply auth
    if (config.auth.type !== 'none') {
      switch (config.auth.type) {
        case 'bearer':
          headers['Authorization'] = `Bearer ${config.auth.token}`;
          break;
        case 'basic':
          const credentials = Buffer.from(
            `${config.auth.username}:${config.auth.password}`
          ).toString('base64');
          headers['Authorization'] = `Basic ${credentials}`;
          break;
        case 'api_key':
          headers[config.auth.header_name || 'X-API-Key'] = config.auth.api_key;
          break;
      }
    }

    // Apply header overrides
    if (headersOverride) {
      Object.assign(headers, headersOverride);
    }

    const requestConfig: AxiosRequestConfig = {
      url: config.url,
      method: config.method,
      headers,
      timeout: config.timeout,
      maxRedirects: config.response_handling.follow_redirects ? 5 : 0,
    };

    // Add body for methods that support it
    if (['POST', 'PUT', 'PATCH'].includes(config.method) && data) {
      if (typeof data === 'object') {
        requestConfig.data = JSON.stringify(data);
      } else {
        requestConfig.data = data;
      }
    }

    // Add query parameters for GET/HEAD
    if (['GET', 'HEAD'].includes(config.method) && data) {
      requestConfig.params = data;
    }

    return requestConfig;
  }

  private async executeWithRetry(
    config: AxiosRequestConfig,
    retryConfig: ApiCallConfig['retry']
  ): Promise<AxiosResponse> {
    const maxAttempts = retryConfig.enabled ? retryConfig.max_attempts : 1;
    let lastError: Error | null = null;

    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      try {
        return await axios(config);
      } catch (error) {
        lastError = error as Error;

        // Don't retry on client errors (4xx)
        if (axios.isAxiosError(error) && error.response?.status && error.response.status < 500) {
          throw error;
        }

        // Wait before retry (except on last attempt)
        if (attempt < maxAttempts) {
          await this.sleep(retryConfig.delay * attempt); // Exponential backoff
        }
      }
    }

    throw lastError;
  }

  private sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  private processResponse(
    response: AxiosResponse,
    handling: ApiCallConfig['response_handling']
  ) {
    const success = response.status >= 200 && response.status < 300;
    let body: any = response.data;

    // Parse JSON response if enabled and content type is JSON
    if (handling.parse_json && typeof body === 'string') {
      const contentType = response.headers['content-type'] || '';
      if (contentType.includes('application/json')) {
        try {
          body = JSON.parse(body);
        } catch {
          // Keep as string if parsing fails
        }
      }
    }

    return {
      response: {
        status: response.status,
        status_text: response.statusText,
        headers: response.headers,
        body: body,
        size: this.calculateResponseSize(response.data),
        duration: response.headers['x-response-time'] || null
      },
      success
    };
  }

  private calculateResponseSize(data: any): number {
    if (typeof data === 'string') {
      return Buffer.byteLength(data);
    }
    return JSON.stringify(data).length;
  }

  private handleError(error: unknown): Error {
    if (axios.isAxiosError(error)) {
      if (error.response) {
        return new Error(
          `API request failed with status ${error.response.status}: ${error.response.statusText}`
        );
      } else if (error.request) {
        return new Error(`API request failed: No response received - ${error.message}`);
      }
    }

    return error instanceof Error ? error : new Error(String(error));
  }
}

// Export node definition for registration
export const nodeDefinition = {
  id: 'api-call',
  executor: ApiCallExecutor
};
