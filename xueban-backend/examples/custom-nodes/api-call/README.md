# API Call Node

The API Call node enables workflows to make REST API calls with full configurability.

## Features

- **Multiple HTTP Methods**: GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS
- **Custom Headers**: Configure request headers with override capability
- **Authentication**: Supports Bearer token, Basic auth, and API key auth
- **Retry Logic**: Configurable retry with exponential backoff
- **Response Handling**: Automatic JSON parsing and SSL configuration
- **Timeout Control**: Configurable request timeout

## Configuration

### Basic Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | string | required | API endpoint URL |
| `method` | string | GET | HTTP method (GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS) |
| `headers` | object | `{ "Content-Type": "application/json" }` | Request headers |
| `timeout` | number | 30000 | Request timeout in milliseconds (1000-120000) |

### Authentication

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `auth.type` | string | none | Auth type: none, bearer, basic, api_key |
| `auth.token` | string | - | Bearer token (for bearer auth) |
| `auth.username` | string | - | Username (for basic auth) |
| `auth.password` | string | - | Password (for basic auth) |
| `auth.api_key` | string | - | API key value |
| `auth.header_name` | string | X-API-Key | Header name for API key |

### Retry Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `retry.enabled` | boolean | false | Enable retry logic |
| `retry.max_attempts` | number | 3 | Maximum retry attempts |
| `retry.delay` | number | 1000 | Base delay between retries (ms) |

### Response Handling

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `response_handling.parse_json` | boolean | true | Automatically parse JSON responses |
| `response_handling.ignore_ssl_errors` | boolean | false | Ignore SSL certificate errors |
| `response_handling.follow_redirects` | boolean | true | Follow HTTP redirects |

## Inputs

| Input | Type | Required | Description |
|-------|------|----------|-------------|
| `data` | object | false | Data to include in request body (POST/PUT/PATCH) or query parameters (GET/HEAD) |
| `headers_override` | object | false | Override headers configured in node settings |

## Outputs

| Output | Type | Description |
|--------|------|-------------|
| `response` | object | Complete API response including status, headers, and body |
| `success` | boolean | True if request was successful (2xx status) |
| `error` | string | Error message if request failed |

## Usage Examples

### Example 1: Simple GET Request

```yaml
nodes:
  - id: api_call
    type: api-call
    config:
      url: https://api.example.com/users
      method: GET
      headers:
        Accept: application/json
```

### Example 2: POST with Bearer Auth

```yaml
nodes:
  - id: create_user
    type: api-call
    config:
      url: https://api.example.com/users
      method: POST
      headers:
        Content-Type: application/json
      auth:
        type: bearer
        token: your-access-token-here
    inputs:
      data:
        name: John Doe
        email: john@example.com
```

### Example 3: API Key Auth with Retry

```yaml
nodes:
  - id: fetch_data
    type: api-call
    config:
      url: https://api.example.com/data
      method: GET
      auth:
        type: api_key
        api_key: your-api-key-here
        header_name: X-API-Key
      retry:
        enabled: true
        max_attempts: 5
        delay: 2000
```

### Example 4: POST from Previous Node Output

```yaml
nodes:
  - id: prepare_data
    type: data-transform
    outputs:
      user_data: { name: "Jane", role: "admin" }

  - id: send_to_api
    type: api-call
    inputs:
      data: $prepare_data.user_data  # Reference previous node output
    config:
      url: https://api.example.com/users
      method: POST
```

## Error Handling

The node provides detailed error information:

- **4xx Errors**: Client errors are not retried
- **5xx Errors**: Server errors are retried if retry is enabled
- **Network Errors**: Retried if retry is enabled
- **Timeout Errors**: Thrown after configured timeout

The `success` output can be used to branch workflow logic:

```yaml
nodes:
  - id: check_api_response
    type: branch
    config:
      condition: $api_call.success
```

## Best Practices

1. **Use Environment Variables** for sensitive data like API keys
2. **Set Appropriate Timeouts** based on expected API response times
3. **Enable Retry** for unreliable external APIs
4. **Parse Response** for structured data to use in downstream nodes
5. **Monitor Rate Limits** and implement delays if needed

## Dependencies

This node requires the `axios` package:

```bash
npm install axios@^1.6.0
```

## License

This custom node is part of AgentGraph and follows the same license.
