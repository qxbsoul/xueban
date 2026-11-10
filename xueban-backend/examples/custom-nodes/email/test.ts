// Email Node Test Suite
// Tests for email sending functionality

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import nodemailer from 'nodemailer';
import EmailExecutor, { nodeDefinition } from './executor';
import { NodeContext } from 'agentgraph';

// Mock nodemailer
vi.mock('nodemailer', () => ({
  createTransport: vi.fn(() => ({
    sendMail: vi.fn()
  }))
}));

describe('EmailExecutor', () => {
  let executor: EmailExecutor;
  let mockNode: any;
  const mockConfig = {
    smtp: {
      host: 'smtp.example.com',
      port: 587,
      secure: false,
      auth: {
        user: 'test@example.com',
        pass: 'password123'
      }
    },
    from: {
      name: 'Test Sender',
      address: 'noreply@example.com'
    },
    content_type: 'auto' as const,
    template: {
      enabled: false,
      engine: 'handlebars' as const
    },
    priority: 'normal' as const,
    tracking: {
      enabled: false,
      open_pixel: false,
      click_tracking: false
    },
    retry: {
      enabled: true,
      max_attempts: 3,
      delay: 5000
    },
    dry_run: false
  };

  beforeEach(() => {
    mockNode = {
      id: 'test-email',
      inputs: [],
      outputs: []
    };
    executor = new EmailExecutor(mockNode);
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('Node Definition', () => {
    it('should export correct node definition', () => {
      expect(nodeDefinition.id).toBe('email');
      expect(nodeDefinition.executor).toBe(EmailExecutor);
    });
  });

  describe('Simple Text Email', () => {
    it('should send simple text email', async () => {
      const mockSendResult = { messageId: '<test@example.com>' };
      vi.mocked(nodemailer.createTransport).mockReturnValueOnce({
        sendMail: vi.fn().mockResolvedValueOnce(mockSendResult)
      } as any);

      const context: NodeContext = {
        inputs: {
          to: ['recipient@example.com'],
          subject: 'Test Subject',
          body: 'Test body text'
        },
        config: mockConfig,
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.success).toBe(true);
      expect(result.outputs.message_id).toBe('<test@example.com>');
      expect(result.outputs.recipients.to_count).toBe(1);
      expect(result.outputs.recipients.total).toBe(1);
    });
  });

  describe('HTML Email', () => {
    it('should send HTML email', async () => {
      const mockSendResult = { messageId: '<html@example.com>' };
      vi.mocked(nodemailer.createTransport).mockReturnValueOnce({
        sendMail: vi.fn().mockResolvedValueOnce(mockSendResult)
      } as any);

      const context: NodeContext = {
        inputs: {
          to: ['recipient@example.com'],
          subject: 'HTML Test',
          body: '<h1>HTML Email</h1><p>This is HTML content</p>'
        },
        config: {
          ...mockConfig,
          content_type: 'text/html'
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.success).toBe(true);
    });

    it('should auto-detect HTML content', async () => {
      const mockSendResult = { messageId: '<auto@example.com>' };
      vi.mocked(nodemailer.createTransport).mockReturnValueOnce({
        sendMail: vi.fn().mockResolvedValueOnce(mockSendResult)
      } as any);

      const context: NodeContext = {
        inputs: {
          to: ['recipient@example.com'],
          subject: 'Auto HTML',
          body: '<h1>HTML Email</h1>'
        },
        config: mockConfig,
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
    });
  });

  describe('Multiple Recipients', () => {
    it('should send to multiple recipients', async () => {
      const mockSendResult = { messageId: '<multi@example.com>' };
      vi.mocked(nodemailer.createTransport).mockReturnValueOnce({
        sendMail: vi.fn().mockResolvedValueOnce(mockSendResult)
      } as any);

      const context: NodeContext = {
        inputs: {
          to: ['alice@example.com', 'bob@example.com'],
          cc: ['manager@example.com'],
          bcc: ['archive@example.com'],
          subject: 'Group Email',
          body: 'Group message'
        },
        config: mockConfig,
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.recipients.to_count).toBe(2);
      expect(result.outputs.recipients.cc_count).toBe(1);
      expect(result.outputs.recipients.bcc_count).toBe(1);
      expect(result.outputs.recipients.total).toBe(4);
    });

    it('should handle single recipient as string', async () => {
      const mockSendResult = { messageId: '<single@example.com>' };
      vi.mocked(nodemailer.createTransport).mockReturnValueOnce({
        sendMail: vi.fn().mockResolvedValueOnce(mockSendResult)
      } as any);

      const context: NodeContext = {
        inputs: {
          to: 'single@example.com',
          subject: 'Single Recipient',
          body: 'Message'
        },
        config: mockConfig,
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.recipients.to_count).toBe(1);
    });
  });

  describe('Template Mode', () => {
    it('should render Handlebars template', async () => {
      const mockSendResult = { messageId: '<template@example.com>' };
      vi.mocked(nodemailer.createTransport).mockReturnValueOnce({
        sendMail: vi.fn().mockResolvedValueOnce(mockSendResult)
      } as any);

      const context: NodeContext = {
        inputs: {
          to: ['recipient@example.com'],
          template_data: {
            name: 'Alice',
            message: 'Welcome!'
          }
        },
        config: {
          ...mockConfig,
          template: {
            enabled: true,
            engine: 'handlebars',
            subject: 'Hello, {{name}}!',
            html: '<h1>{{message}}</h1>',
            text: '{{message}}'
          }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.success).toBe(true);
    });

    it('should render Mustache template', async () => {
      const mockSendResult = { messageId: '<mustache@example.com>' };
      vi.mocked(nodemailer.createTransport).mockReturnValueOnce({
        sendMail: vi.fn().mockResolvedValueOnce(mockSendResult)
      } as any);

      const context: NodeContext = {
        inputs: {
          to: ['recipient@example.com'],
          template_data: {
            name: 'Bob',
            message: 'Hello!'
          }
        },
        config: {
          ...mockConfig,
          template: {
            enabled: true,
            engine: 'mustache',
            subject: 'Hello, {{name}}!',
            html: '<p>{{message}}</p>'
          }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
    });

    it('should render simple template', async () => {
      const mockSendResult = { messageId: '<simple@example.com>' };
      vi.mocked(nodemailer.createTransport).mockReturnValueOnce({
        sendMail: vi.fn().mockResolvedValueOnce(mockSendResult)
      } as any);

      const context: NodeContext = {
        inputs: {
          to: ['recipient@example.com'],
          template_data: {
            name: 'Charlie',
            balance: 100
          }
        },
        config: {
          ...mockConfig,
          template: {
            enabled: true,
            engine: 'simple',
            subject: 'Hello, ${name}!',
            html: '<p>Balance: ${balance}</p>'
          }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
    });
  });

  describe('Dry Run Mode', () => {
    it('should simulate sending in dry run mode', async () => {
      const context: NodeContext = {
        inputs: {
          to: ['recipient@example.com'],
          subject: 'Dry Run Test',
          body: 'This will not actually send'
        },
        config: {
          ...mockConfig,
          dry_run: true
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(result.outputs.success).toBe(true);
      expect(result.outputs.dry_run).toBe(true);
      expect(result.outputs.message_id).toMatch(/^dry-run-/);
      expect(result.outputs.preview).toBeDefined();
      expect(nodemailer.createTransport).not.toHaveBeenCalled();
    });
  });

  describe('Priority Levels', () => {
    it('should send high priority email', async () => {
      const mockSendResult = { messageId: '<high@example.com>' };
      const mockTransporter = {
        sendMail: vi.fn().mockResolvedValueOnce(mockSendResult)
      };
      vi.mocked(nodemailer.createTransport).mockReturnValueOnce(mockTransporter as any);

      const context: NodeContext = {
        inputs: {
          to: ['recipient@example.com'],
          subject: 'Urgent',
          body: 'Important message'
        },
        config: {
          ...mockConfig,
          priority: 'high'
        },
        state: {} as any
      };

      await executor.execute(context);

      expect(mockTransporter.sendMail).toHaveBeenCalledWith(
        expect.objectContaining({
          priority: 'HIGH'
        })
      );
    });
  });

  describe('Retry Logic', () => {
    it('should retry on failure', async () => {
      let attempts = 0;
      const mockTransporter = {
        sendMail: vi.fn().mockImplementation(() => {
          attempts++;
          if (attempts < 3) {
            return Promise.reject(new Error('Network error'));
          }
          return Promise.resolve({ messageId: '<retry@example.com>' });
        })
      };
      vi.mocked(nodemailer.createTransport).mockReturnValueOnce(mockTransporter as any);

      const context: NodeContext = {
        inputs: {
          to: ['recipient@example.com'],
          subject: 'Retry Test',
          body: 'Test'
        },
        config: {
          ...mockConfig,
          retry: {
            enabled: true,
            max_attempts: 3,
            delay: 100
          }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(true);
      expect(attempts).toBe(3);
    });
  });

  describe('Error Handling', () => {
    it('should validate email addresses', async () => {
      const context: NodeContext = {
        inputs: {
          to: ['invalid-email'],
          subject: 'Test',
          body: 'Test'
        },
        config: mockConfig,
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(false);
      expect(result.error?.message).toContain('Invalid email');
    });

    it('should handle send failure', async () => {
      const mockTransporter = {
        sendMail: vi.fn().mockRejectedValueOnce(new Error('SMTP error'))
      };
      vi.mocked(nodemailer.createTransport).mockReturnValueOnce(mockTransporter as any);

      const context: NodeContext = {
        inputs: {
          to: ['recipient@example.com'],
          subject: 'Test',
          body: 'Test'
        },
        config: {
          ...mockConfig,
          retry: {
            enabled: false,
            max_attempts: 1,
            delay: 5000
          }
        },
        state: {} as any
      };

      const result = await executor.execute(context);

      expect(result.success).toBe(false);
      expect(result.error?.message).toContain('SMTP error');
    });
  });

  describe('Email Validation', () => {
    it('should accept valid email addresses', async () => {
      const mockSendResult = { messageId: '<valid@example.com>' };
      vi.mocked(nodemailer.createTransport).mockReturnValueOnce({
        sendMail: vi.fn().mockResolvedValueOnce(mockSendResult)
      } as any);

      const validEmails = [
        'test@example.com',
        'user.name@example.com',
        'user+tag@example.co.uk'
      ];

      for (const email of validEmails) {
        const context: NodeContext = {
          inputs: {
            to: [email],
            subject: 'Test',
            body: 'Test'
          },
          config: mockConfig,
          state: {} as any
        };

        const result = await executor.execute(context);
        expect(result.success).toBe(true);
      }
    });

    it('should reject invalid email addresses', async () => {
      const invalidEmails = [
        'invalid',
        '@example.com',
        'user@',
        'user @example.com'
      ];

      for (const email of invalidEmails) {
        const context: NodeContext = {
          inputs: {
            to: [email],
            subject: 'Test',
            body: 'Test'
          },
          config: mockConfig,
          state: {} as any
        };

        const result = await executor.execute(context);
        expect(result.success).toBe(false);
        expect(result.error?.message).toContain('Invalid email');
      }
    });
  });
});
