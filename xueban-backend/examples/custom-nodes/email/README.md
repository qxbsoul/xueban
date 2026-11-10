# Email Node

The Email node enables sending email notifications via SMTP with support for HTML templates, attachments, and multiple recipients.

## Features

- **SMTP Support**: Full SMTP configuration with authentication
- **Multiple Recipients**: To, CC, and BCC support
- **HTML Templates**: Handlebars, Mustache, or simple template engines
- **Attachments**: File attachments with base64 encoding
- **Email Tracking**: Optional open and click tracking
- **Priority Levels**: Low, normal, and high priority emails
- **Retry Logic**: Automatic retry on send failures
- **Dry Run**: Test email sending without actually sending

## Configuration

### SMTP Configuration

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `smtp.host` | string | yes | SMTP server hostname |
| `smtp.port` | integer | no | SMTP port (default: 587) |
| `smtp.secure` | boolean | no | Use SSL/TLS (default: false) |
| `smtp.auth.user` | string | yes | SMTP username |
| `smtp.auth.pass` | string | yes | SMTP password |

### Sender Configuration

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `from.name` | string | no | Sender display name |
| `from.address` | string | yes | Sender email address |
| `reply_to` | string | no | Reply-to address |

### Content Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `content_type` | string | auto | Content type: text/plain, text/html, auto |
| `priority` | string | normal | Email priority: low, normal, high |

### Template Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `template.enabled` | boolean | false | Enable template mode |
| `template.engine` | string | handlebars | Template engine: handlebars, mustache, simple |
| `template.subject` | string | - | Subject template |
| `template.html` | string | - | HTML body template |
| `template.text` | string | - | Text body template |

### Retry Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `retry.enabled` | boolean | true | Enable retry logic |
| `retry.max_attempts` | integer | 3 | Maximum retry attempts |
| `retry.delay` | integer | 5000 | Retry delay in milliseconds |

## Inputs

| Input | Type | Required | Description |
|-------|------|----------|-------------|
| `to` | array/string | false* | Recipient email addresses |
| `cc` | array/string | false | CC recipients |
| `bcc` | array/string | false | BCC recipients |
| `subject` | string | false* | Email subject |
| `body` | string | false* | Email body (text or HTML) |
| `template_data` | object | false | Data for template rendering |
| `attachments` | array | false | Attachment objects |

* Required when not using template mode

### Attachment Format

```typescript
{
  filename: string;      // File name
  content: string;       // Base64 encoded content
  content_type?: string; // MIME type
  encoding?: string;     // Encoding (default: base64)
}
```

## Outputs

| Output | Type | Description |
|--------|------|-------------|
| `success` | boolean | True if email was sent successfully |
| `message_id` | string | Message ID from SMTP server |
| `recipients` | object | Recipient summary |
| `error` | string | Error message if sending failed |

## Usage Examples

### Example 1: Simple Text Email

```yaml
nodes:
  - id: send_email
    type: email
    config:
      smtp:
        host: "smtp.example.com"
        port: 587
        secure: false
        auth:
          user: "user@example.com"
          pass: "${SMTP_PASSWORD}"  # Use environment variable
      from:
        name: "AgentGraph"
        address: "noreply@example.com"
    inputs:
      to: ["user@example.com"]
      subject: "Hello World"
      body: "This is a simple email message."
```

### Example 2: HTML Email

```yaml
nodes:
  - id: send_html_email
    type: email
    config:
      smtp:
        host: "smtp.example.com"
        port: 587
        secure: false
        auth:
          user: "user@example.com"
          pass: "${SMTP_PASSWORD}"
      from:
        name: "AgentGraph"
        address: "noreply@example.com"
      content_type: text/html
    inputs:
      to: ["user@example.com"]
      subject: "Welcome"
      body: |
        <html>
          <body>
            <h1>Welcome to AgentGraph</h1>
            <p>Thank you for signing up!</p>
          </body>
        </html>
```

### Example 3: Email with Template

```yaml
nodes:
  - id: send_templated_email
    type: email
    config:
      smtp:
        host: "smtp.example.com"
        port: 587
        secure: false
        auth:
          user: "user@example.com"
          pass: "${SMTP_PASSWORD}"
      from:
        name: "AgentGraph"
        address: "noreply@example.com"
      template:
        enabled: true
        engine: handlebars
        subject: "Welcome, {{name}}!"
        html: |
          <html>
            <body>
              <h1>Hello, {{name}}!</h1>
              <p>{{message}}</p>
            </body>
          </html>
    inputs:
      to: ["user@example.com"]
      template_data:
        name: "Alice"
        message: "Welcome to our platform!"
```

### Example 4: Email with Attachments

```yaml
nodes:
  - id: send_attachment
    type: email
    config:
      smtp:
        host: "smtp.example.com"
        port: 587
        secure: false
        auth:
          user: "user@example.com"
          pass: "${SMTP_PASSWORD}"
      from:
        name: "AgentGraph"
        address: "noreply@example.com"
    inputs:
      to: ["user@example.com"]
      subject: "Report Attached"
      body: "Please find the report attached."
      attachments:
        - filename: "report.pdf"
          content: "{{base64_encoded_pdf}}"
          content_type: "application/pdf"
```

### Example 5: Multiple Recipients

```yaml
nodes:
  - id: send_group_email
    type: email
    config:
      smtp:
        host: "smtp.example.com"
        port: 587
        secure: false
        auth:
          user: "user@example.com"
          pass: "${SMTP_PASSWORD}"
      from:
        name: "AgentGraph"
        address: "noreply@example.com"
    inputs:
      to: ["alice@example.com", "bob@example.com"]
      cc: ["manager@example.com"]
      bcc: ["archive@example.com"]
      subject: "Team Update"
      body: "Here is the weekly team update."
```

### Example 6: Dry Run (Testing)

```yaml
nodes:
  - id: test_email
    type: email
    config:
      smtp:
        host: "smtp.example.com"
        port: 587
        secure: false
        auth:
          user: "user@example.com"
          pass: "${SMTP_PASSWORD}"
      from:
        name: "AgentGraph"
        address: "noreply@example.com"
      dry_run: true
    inputs:
      to: ["test@example.com"]
      subject: "Test Email"
      body: "This is a test - will not actually send."
```

### Example 7: Using Mustache Templates

```yaml
nodes:
  - id: send_mustache_email
    type: email
    config:
      smtp:
        host: "smtp.example.com"
        port: 587
        secure: false
        auth:
          user: "user@example.com"
          pass: "${SMTP_PASSWORD}"
      from:
        name: "AgentGraph"
        address: "noreply@example.com"
      template:
        enabled: true
        engine: mustache
        subject: "Order Confirmation #{{order_id}}"
        html: |
          <h1>Order #{{order_id}}</h1>
          <p>Thank you for your purchase!</p>
          <p>Total: {{amount}}</p>
    inputs:
      to: ["customer@example.com"]
      template_data:
        order_id: "12345"
        amount: "$99.99"
```

### Example 8: High Priority Email

```yaml
nodes:
  - id: urgent_notification
    type: email
    config:
      smtp:
        host: "smtp.example.com"
        port: 587
        secure: false
        auth:
          user: "user@example.com"
          pass: "${SMTP_PASSWORD}"
      from:
        name: "System Alerts"
        address: "alerts@example.com"
      priority: high
      retry:
        enabled: true
        max_attempts: 5
        delay: 10000
    inputs:
      to: ["admin@example.com"]
      subject: "URGENT: System Alert"
      body: "A critical system error has occurred."
```

## Template Syntax

### Handlebars

```html
<h1>Hello, {{name}}!</h1>

{{#if premium}}
  <p>You are a premium member!</p>
{{/if}}

<ul>
  {{#each items}}
    <li>{{this}}</li>
  {{/each}}
</ul>
```

### Mustache

```html
<h1>Hello, {{name}}!</h1>

{{#premium}}
  <p>You are a premium member!</p>
{{/premium}}

<ul>
  {{#items}}
    <li>{{.}}</li>
  {{/items}}
</ul>
```

### Simple

```html
<h1>Hello, ${name}!</h1>
<p>Your balance is: ${balance}</p>
```

## Common SMTP Providers

| Provider | Host | Port | Secure |
|----------|------|------|--------|
| Gmail | smtp.gmail.com | 587 | false |
| Outlook | smtp-mail.outlook.com | 587 | false |
| SendGrid | smtp.sendgrid.net | 587 | false |
| Amazon SES | email-smtp.us-east-1.amazonaws.com | 587 | false |
| Mailgun | smtp.mailgun.org | 587 | false |

## Security Best Practices

1. **Use Environment Variables** for credentials
2. **Enable SSL/TLS** when available
3. **Validate Email Addresses** before sending
4. **Use Dry Run** for testing
5. **Limit Retry Attempts** to avoid spam
6. **Monitor Bounce Rates** for deliverability

## Error Handling

The node provides detailed error information:

| Error | Cause |
|-------|-------|
| Invalid email address | Recipient email format is invalid |
| Authentication failed | Incorrect SMTP credentials |
| Connection timeout | SMTP server unreachable |
| Rate limit exceeded | Too many emails sent |

## Dependencies

This node requires the following packages:

```bash
npm install nodemailer@^6.9.0 handlebars@^4.7.0 mustache@^4.2.0
```

## License

This custom node is part of AgentGraph and follows the same license.
