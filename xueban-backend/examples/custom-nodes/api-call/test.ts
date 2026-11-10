// API Call Node Test Suite
// Tests for API Call executor functionality

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import axios from 'axios';
import ApiCallExecutor, { nodeDefinition } from './executor';
import { NodeContext } from 'agentgraph';

// Mock axios
vi.mock('axios');

describe('ApiCallExecutor', () => {
  let executor: ApiCallExecutor;
  let mockNode: any;

  beforeEach(() => {
    mockNode = {
      id: 'api-call',
      inputs: [],
      outputs: []
    };
    executor = new ApiCallExecutor(mockNode);
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('Node Definition', () => {
    it('should export correct node definition', () => {
      expect(nodeDefinition.id).toBe('api-call');
      expect(nodeDefinition.executor).toBe(ApiCallExecutor);
    });
  });

  describe('Basic GET Request', () => {
    it('should execute a successful GET request', async () => {
      const mockResponse = {
        data: { id: 1, name: 'Test' },
        status: 200,
        statusText: 'OK',
        headers: { 'content-type': 'application/json' }
      };
      vi.mocked(axios).mockResolvedValueOnce(mockResponse);

      const context: NodeContext = {
        inputs: {},
        config: {
          url: 'https://api.example.com/test',
          method: 'GET',
          headers: {},
          timeout: 30000,
          retry: { enabled: false, max_attempts: 3, delay: 1000 },
          auth: { type: 'none' },
          response_handling: { parse_json: true, ignore_ssl_errors: false, follow_redirects: true }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.success).toBe(true);
      expect(result.outputs.response.body).toEqual({ id: 1, name: 'Test' });
      expect(axios).toHaveBeenCalledWith(
        expect.objectContaining({
          url: 'https://api.example.com/test',
          method: 'GET'
        })
      );
    });
  });

  describe('POST Request', () => {
    it('should send POST request with JSON body', async () => {
      const mockResponse = {
        data: { id: 2, created: true },
        status: 201,
        statusText: 'Created',
        headers: { 'content-type': 'application/json' }
      };
      vi.mocked(axios).mockResolvedValueOnce(mockResponse);

      const context: NodeContext = {
        inputs: {
          data: { name: 'New User', email: 'test@example.com' }
        },
        config: {
          url: 'https://api.example.com/users',
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          timeout: 30000,
          retry: { enabled: false, max_attempts: 3, delay: 1000 },
          auth: { type: 'none' },
          response_handling: { parse_json: true, ignore_ssl_errors: false, follow_redirects: true }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(axios).toHaveBeenCalledWith(
        expect.objectContaining({
          method: 'POST',
          data: JSON.stringify({ name: 'New User', email: 'test@example.com' })
        })
      );
    });
  });

  describe('Authentication', () => {
    it('should add Bearer token to headers', async () => {
      const mockResponse = {
        data: { user: 'authenticated' },
        status: 200,
        statusText: 'OK',
        headers: {}
      };
      vi.mocked(axios).mockResolvedValueOnce(mockResponse);

      const context: NodeContext = {
        inputs: {},
        config: {
          url: 'https://api.example.com/protected',
          method: 'GET',
          headers: {},
          timeout: 30000,
          retry: { enabled: false, max_attempts: 3, delay: 1000 },
          auth: { type: 'bearer', token: 'test-token-123' },
          response_handling: { parse_json: true, ignore_ssl_errors: false, follow_redirects: true }
        },
        state: {} as any
      };

      await executor.execute(context);

      expect(axios).toHaveBeenCalledWith(
        expect.objectContaining({
          headers: expect.objectContaining({
            Authorization: 'Bearer test-token-123'
          })
        })
      );
    });

    it('should add Basic auth credentials', async () => {
      const mockResponse = {
        data: { user: 'authenticated' },
        status: 200,
        statusText: 'OK',
        headers: {}
      };
      vi.mocked(axios).mockResolvedValueOnce(mockResponse);

      const context: NodeContext = {
        inputs: {},
        config: {
          url: 'https://api.example.com/protected',
          method: 'GET',
          headers: {},
          timeout: 30000,
          retry: { enabled: false, max_attempts: 3, delay: 1000 },
          auth: { type: 'basic', username: 'user', password: 'pass' },
          response_handling: { parse_json: true, ignore_ssl_errors: false, follow_redirects: true }
        },
        state: {} as any
      };

      await executor.execute(context);

      const call = vi.mocked(axios).mock.calls[0][0];
      expect(call.headers.Authorization).toMatch(/^Basic /);
    });

    it('should add API key to custom header', async () => {
      const mockResponse = {
        data: { user: 'authenticated' },
        status: 200,
        statusText: 'OK',
        headers: {}
      };
      vi.mocked(axios).mockResolvedValueOnce(mockResponse);

      const context: NodeContext = {
        inputs: {},
        config: {
          url: 'https://api.example.com/protected',
          method: 'GET',
          headers: {},
          timeout: 30000,
          retry: { enabled: false, max_attempts: 3, delay: 1000 },
          auth: { type: 'api_key', api_key: 'my-secret-key', header_name: 'X-Custom-API-Key' },
          response_handling: { parse_json: true, ignore_ssl_errors: false, follow_redirects: true }
        },
        state: {} as any
      };

      await executor.execute(context);

      expect(axios).toHaveBeenCalledWith(
        expect.objectContaining({
          headers: expect.objectContaining({
            'X-Custom-API-Key': 'my-secret-key'
          })
        })
      );
    });
  });

  describe('Retry Logic', () => {
    it('should retry on server error', async () => {
      const mockResponse = {
        data: { result: 'success' },
        status: 200,
        statusText: 'OK',
        headers: {}
      };

      vi.mocked(axios)
        .mockRejectedValueOnce(new Error('Network error'))
        .mockRejectedValueOnce(new Error('Network error'))
        .mockResolvedValueOnce(mockResponse);

      const context: NodeContext = {
        inputs: {},
        config: {
          url: 'https://api.example.com/test',
          method: 'GET',
          headers: {},
          timeout: 30000,
          retry: { enabled: true, max_attempts: 3, delay: 100 },
          auth: { type: 'none' },
          response_handling: { parse_json: true, ignore_ssl_errors: false, follow_redirects: true }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(axios).toHaveBeenCalledTimes(3);
    });

    it('should not retry on client error (4xx)', async () => {
      const error = new Error('Not Found');
      (error as any).response = { status: 404, statusText: 'Not Found' };
      vi.mocked(axios).mockRejectedValueOnce(error);

      const context: NodeContext = {
        inputs: {},
        config: {
          url: 'https://api.example.com/notfound',
          method: 'GET',
          headers: {},
          timeout: 30000,
          retry: { enabled: true, max_attempts: 3, delay: 100 },
          auth: { type: 'none' },
          response_handling: { parse_json: true, ignore_ssl_errors: false, follow_redirects: true }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(false);
      expect(axios).toHaveBeenCalledTimes(1);
    });
  });

  describe('Header Override', () => {
    it('should allow overriding headers from input', async () => {
      const mockResponse = {
        data: { result: 'ok' },
        status: 200,
        statusText: 'OK',
        headers: {}
      };
      vi.mocked(axios).mockResolvedValueOnce(mockResponse);

      const context: NodeContext = {
        inputs: {
          headers_override: { 'X-Custom-Header': 'custom-value' }
        },
        config: {
          url: 'https://api.example.com/test',
          method: 'GET',
          headers: { 'Content-Type': 'application/json' },
          timeout: 30000,
          retry: { enabled: false, max_attempts: 3, delay: 1000 },
          auth: { type: 'none' },
          response_handling: { parse_json: true, ignore_ssl_errors: false, follow_redirects: true }
        },
        state: {} as any
      };

      await executor.execute(context);

      expect(axios).toHaveBeenCalledWith(
        expect.objectContaining({
          headers: expect.objectContaining({
            'Content-Type': 'application/json',
            'X-Custom-Header': 'custom-value'
          })
        })
      );
    });
  });

  describe('Error Handling', () => {
    it('should handle network errors', async () => {
      vi.mocked(axios).mockRejectedValueOnce(new Error('Network Error'));

      const context: NodeContext = {
        inputs: {},
        config: {
          url: 'https://api.example.com/test',
          method: 'GET',
          headers: {},
          timeout: 30000,
          retry: { enabled: false, max_attempts: 3, delay: 1000 },
          auth: { type: 'none' },
          response_handling: { parse_json: true, ignore_ssl_errors: false, follow_redirects: true }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(false);
      expect(result.error).toBeDefined();
    });

    it('should handle timeout errors', async () => {
      vi.mocked(axios).mockRejectedValueOnce(new Error('timeout of 1000ms exceeded'));

      const context: NodeContext = {
        inputs: {},
        config: {
          url: 'https://api.example.com/slow',
          method: 'GET',
          headers: {},
          timeout: 1000,
          retry: { enabled: false, max_attempts: 3, delay: 1000 },
          auth: { type: 'none' },
          response_handling: { parse_json: true, ignore_ssl_errors: false, follow_redirects: true }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(false);
    });
  });

  describe('Response Processing', () => {
    it('should calculate response size', async () => {
      const mockResponse = {
        data: { large: 'data'.repeat(100) },
        status: 200,
        statusText: 'OK',
        headers: {}
      };
      vi.mocked(axios).mockResolvedValueOnce(mockResponse);

      const context: NodeContext = {
        inputs: {},
        config: {
          url: 'https://api.example.com/test',
          method: 'GET',
          headers: {},
          timeout: 30000,
          retry: { enabled: false, max_attempts: 3, delay: 1000 },
          auth: { type: 'none' },
          response_handling: { parse_json: true, ignore_ssl_errors: false, follow_redirects: true }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.outputs.response.size).toBeGreaterThan(0);
    });
  });
});
