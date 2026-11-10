// Email Node Executor
// Sends email notifications via SMTP with template and attachment support

import nodemailer, { Transporter, SentMessageInfo } from 'nodemailer';
import Handlebars from 'handlebars';
import Mustache from 'mustache';
import { BaseNodeExecutor, NodeContext, NodeResult } from 'agentgraph';

interface EmailConfig {
  smtp: {
    host: string;
    port: number;
    secure: boolean;
    auth: {
      user: string;
      pass: string;
    };
  };
  from: {
    name: string;
    address: string;
  };
  reply_to?: string;
  content_type: 'text/plain' | 'text/html' | 'auto';
  template: {
    enabled: boolean;
    engine: 'handlebars' | 'mustache' | 'simple';
    subject?: string;
    html?: string;
    text?: string;
  };
  priority: 'low' | 'normal' | 'high';
  tracking: {
    enabled: boolean;
    open_pixel: boolean;
    click_tracking: boolean;
  };
  retry: {
    enabled: boolean;
    max_attempts: number;
    delay: number;
  };
  dry_run: boolean;
}

export default class EmailExecutor extends BaseNodeExecutor {
  private transporter: Transporter | null = null;

  async execute(context: NodeContext): Promise<NodeResult> {
    const { to, cc, bcc, subject, body, template_data, attachments } = context.inputs;
    const config = context.config as EmailConfig;

    try {
      // 1. Validate inputs
      this.validateEmailInputs(to, subject, body, config);

      // 2. Prepare email content
      const emailContent = this.prepareEmailContent(
        subject,
        body,
        template_data,
        config
      );

      // 3. Build recipients list
      const recipients = this.buildRecipients(to, cc, bcc, config);

      // 4. Build email options
      const mailOptions = this.buildMailOptions(
        emailContent,
        recipients,
        attachments,
        config
      );

      // 5. Dry run check
      if (config.dry_run) {
        return this.createDryRunResult(recipients, mailOptions);
      }

      // 6. Initialize transporter
      this.transporter = this.createTransporter(config);

      // 7. Send email with retry
      const result = await this.sendWithRetry(mailOptions, config.retry);

      return this.createSuccessResult(result, recipients);

    } catch (error) {
      return this.failure(error instanceof Error ? error : new Error(String(error)));
    }
  }

  private validateEmailInputs(
    to: any,
    subject: any,
    body: any,
    config: EmailConfig
  ): void {
    if (!to && !config.template?.enabled) {
      throw new Error('Recipients (to) are required when not using template mode');
    }

    // Validate email addresses format
    const validateEmailList = (list: any) => {
      if (!list) return [];
      const emails = Array.isArray(list) ? list : [list];
      const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
      for (const email of emails) {
        if (typeof email === 'string' && !emailRegex.test(email)) {
          throw new Error(`Invalid email address: ${email}`);
        }
      }
      return emails;
    };

    validateEmailList(to);
    validateEmailList(cc);
    validateEmailList(bcc);
  }

  private prepareEmailContent(
    subject: any,
    body: any,
    templateData: any,
    config: EmailConfig
  ): { subject: string; html: string | null; text: string | null } {
    let emailSubject = subject || '';
    let htmlBody: string | null = null;
    let textBody: string | null = null;

    if (config.template?.enabled) {
      // Use template
      emailSubject = this.renderTemplate(
        config.template.subject || subject || '',
        templateData,
        config.template.engine
      );

      if (config.template.html) {
        htmlBody = this.renderTemplate(
          config.template.html,
          templateData,
          config.template.engine
        );
      }

      if (config.template.text) {
        textBody = this.renderTemplate(
          config.template.text,
          templateData,
          config.template.engine
        );
      }
    } else {
      // Direct content
      emailSubject = subject || '';

      if (config.content_type === 'auto') {
        // Auto-detect HTML
        htmlBody = typeof body === 'string' && body.includes('<') ? body : null;
        textBody = htmlBody ? null : (body || '');
      } else if (config.content_type === 'text/html') {
        htmlBody = body || '';
      } else {
        textBody = body || '';
      }
    }

    // Apply tracking if enabled
    if (config.tracking.enabled && config.tracking.open_pixel && htmlBody) {
      htmlBody = this.addOpenTrackingPixel(htmlBody, emailSubject);
    }

    return {
      subject: emailSubject,
      html: htmlBody,
      text: textBody
    };
  }

  private renderTemplate(
    template: string,
    data: any,
    engine: 'handlebars' | 'mustache' | 'simple'
  ): string {
    if (!template) return '';

    switch (engine) {
      case 'handlebars':
        return Handlebars.compile(template)(data || {});

      case 'mustache':
        return Mustache.render(template, data || {});

      case 'simple':
        // Simple ${var} substitution
        return template.replace(/\$\{([^}]+)\}/g, (_, key) => {
          return data?.[key] ?? '';
        };

      default:
        return template;
    }
  }

  private buildRecipients(
    to: any,
    cc: any,
    bcc: any,
    config: EmailConfig
  ): { to: string[]; cc: string[]; bcc: string[] } {
    return {
      to: Array.isArray(to) ? to : (to ? [to] : []),
      cc: Array.isArray(cc) ? cc : (cc ? [cc] : []),
      bcc: Array.isArray(bcc) ? bcc : (bcc ? [bcc] : [])
    };
  }

  private buildMailOptions(
    content: { subject: string; html: string | null; text: string | null },
    recipients: { to: string[]; cc: string[]; bcc: string[] },
    attachments: any,
    config: EmailConfig
  ): any {
    const options: any = {
      from: {
        name: config.from.name,
        address: config.from.address
      },
      to: recipients.to.join(', '),
      subject: content.subject,
      priority: config.priority.toUpperCase()
    };

    if (recipients.cc.length > 0) {
      options.cc = recipients.cc.join(', ');
    }

    if (recipients.bcc.length > 0) {
      options.bcc = recipients.bcc.join(', ');
    }

    if (config.reply_to) {
      options.replyTo = config.reply_to;
    }

    if (content.html) {
      options.html = content.html;
    }

    if (content.text) {
      options.text = content.text;
    }

    if (attachments && Array.isArray(attachments)) {
      options.attachments = attachments.map((att: any) => ({
        filename: att.filename || 'attachment',
        content: att.content,
        encoding: att.encoding || 'base64',
        contentType: att.content_type
      }));
    }

    return options;
  }

  private createTransporter(config: EmailConfig): Transporter {
    return nodemailer.createTransport({
      host: config.smtp.host,
      port: config.smtp.port,
      secure: config.smtp.secure,
      auth: {
        user: config.smtp.auth.user,
        pass: config.smtp.auth.pass
      }
    });
  }

  private async sendWithRetry(
    mailOptions: any,
    retryConfig: EmailConfig['retry']
  ): Promise<SentMessageInfo> {
    const maxAttempts = retryConfig.enabled ? retryConfig.max_attempts : 1;
    let lastError: Error | null = null;

    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      try {
        return await this.transporter!.sendMail(mailOptions);
      } catch (error) {
        lastError = error as Error;

        // Wait before retry
        if (attempt < maxAttempts) {
          await this.sleep(retryConfig.delay * attempt);
        }
      }
    }

    throw lastError;
  }

  private sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  private addOpenTrackingPixel(html: string, subject: string): string {
    // Add transparent tracking pixel
    const trackingUrl = `https://track.example.com/pixel?subject=${encodeURIComponent(subject)}`;
    const pixel = `<img src="${trackingUrl}" width="1" height="1" style="display:none;" alt="" />`;
    return html.replace('</body>', `${pixel}</body>`) || html + pixel;
  }

  private createSuccessResult(
    result: SentMessageInfo,
    recipients: { to: string[]; cc: string[]; bcc: string[] }
  ): NodeResult {
    return this.success({
      success: true,
      message_id: result.messageId,
      recipients: {
        to_count: recipients.to.length,
        cc_count: recipients.cc.length,
        bcc_count: recipients.bcc.length,
        total: recipients.to.length + recipients.cc.length + recipients.bcc.length
      }
    });
  }

  private createDryRunResult(
    recipients: { to: string[]; cc: string[]; bcc: string[] },
    mailOptions: any
  ): NodeResult {
    return this.success({
      success: true,
      message_id: 'dry-run-' + Date.now(),
      recipients: {
        to_count: recipients.to.length,
        cc_count: recipients.cc.length,
        bcc_count: recipients.bcc.length,
        total: recipients.to.length + recipients.cc.length + recipients.bcc.length
      },
      dry_run: true,
      preview: {
        to: mailOptions.to,
        cc: mailOptions.cc,
        subject: mailOptions.subject,
        has_html: !!mailOptions.html,
        has_attachments: mailOptions.attachments?.length > 0
      }
    });
  }
}

// Export node definition for registration
export const nodeDefinition = {
  id: 'email',
  executor: EmailExecutor
};
