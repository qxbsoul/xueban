# Filter Node

The Filter node enables powerful data filtering based on configurable conditions and custom expressions.

## Features

- **Multiple Modes**: Filter arrays, single values, or object keys
- **Rich Operators**: 14 comparison operators including regex and type checking
- **Custom Expressions**: JavaScript-like expressions for complex filtering
- **Multiple Conditions**: Combine conditions with AND/OR logic
- **Sorting**: Sort filtered results by any field
- **Context Access**: Access external context data in filter expressions

## Configuration

### Filter Modes

| Mode | Description | Input Type |
|------|-------------|------------|
| `array` | Filter array items | Array or single item |
| `single` | Check if single value passes condition | Any value |
| `object` | Filter object keys | Object |

### Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `eq` | Equals | `age: 30` |
| `ne` | Not equals | `status: 'inactive'` |
| `gt` | Greater than | `price: 100` |
| `gte` | Greater or equal | `rating: 4.5` |
| `lt` | Less than | `age: 18` |
| `lte` | Less or equal | `stock: 10` |
| `in` | Value in array | `category: ['tech', 'books']` |
| `not_in` | Value not in array | `status: ['deleted', 'archived']` |
| `contains` | String contains substring | `name: 'john'` |
| `starts_with` | String starts with | `email: 'admin@'` |
| `ends_with` | String ends with | `url: '.com'` |
| `regex` | Regular expression match | `phone: '^\\+1\\d{10}$'` |
| `exists` | Field exists | `description` |
| `type` | Type check | `age: 'number'` |
| `empty` | Value is empty | `tags` |
| `truthy` | Value is truthy | `active` |

### Configuration Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `mode` | string | array | Filter mode |
| `condition` | object | - | Single filter condition |
| `expression` | string | - | Custom filter expression |
| `conditions` | array | [] | Multiple conditions |
| `limit` | number | 0 | Max results (0 = no limit) |
| `sort` | object | - | Sort configuration |
| `on_empty` | string | return_empty | Behavior when no matches |

## Inputs

| Input | Type | Required | Description |
|-------|------|----------|-------------|
| `data` | any | true | Input data to filter |
| `context` | object | false | Additional context for expressions |

## Outputs

| Output | Type | Description |
|--------|------|-------------|
| `filtered_data` | any | Filtered output data |
| `matched_count` | number | Number of items that matched |
| `total_count` | number | Total items processed |
| `passed` | boolean | True if any items passed |

## Usage Examples

### Example 1: Simple Array Filter

```yaml
nodes:
  - id: filter_active_users
    type: filter
    config:
      mode: array
      condition:
        field: "status"
        operator: "eq"
        value: "active"
    inputs:
      data:
        - { id: 1, name: "Alice", status: "active" }
        - { id: 2, name: "Bob", status: "inactive" }
        - { id: 3, name: "Charlie", status: "active" }
```

Output:
```json
{
  "filtered_data": [
    { "id": 1, "name": "Alice", "status": "active" },
    { "id": 3, "name": "Charlie", "status": "active" }
  ],
  "matched_count": 2,
  "total_count": 3,
  "passed": true
}
```

### Example 2: Multiple Conditions with AND/OR

```yaml
nodes:
  - id: filter_products
    type: filter
    config:
      mode: array
      conditions:
        - field: "category"
          operator: "eq"
          value: "electronics"
          logic: "AND"
        - field: "price"
          operator: "gte"
          value: 100
          logic: "AND"
        - field: "brand"
          operator: "in"
          value: ["Apple", "Samsung"]
          logic: "OR"
    inputs:
      data:
        - { name: "iPhone", category: "electronics", price: 999, brand: "Apple" }
        - { name: "TV", category: "electronics", price: 599, brand: "Samsung" }
        - { name: "Book", category: "books", price: 20, brand: "Generic" }
```

### Example 3: Custom Expression

```yaml
nodes:
  - id: filter_complex
    type: filter
    config:
      mode: array
      expression: "item.age >= 18 && item.hasLicense === true"
    inputs:
      data:
        - { name: "Alice", age: 25, hasLicense: true }
        - { name: "Bob", age: 17, hasLicense: true }
        - { name: "Charlie", age: 30, hasLicense: false }
```

### Example 4: Regex Filter

```yaml
nodes:
  - id: filter_emails
    type: filter
    config:
      mode: array
      condition:
        field: "email"
        operator: "regex"
        value: "^[a-z]+@[a-z]+\\.(com|org)$"
        case_sensitive: false
    inputs:
      data:
        - { name: "Alice", email: "alice@example.com" }
        - { name: "Bob", email: "BOB@example.COM" }
        - { name: "Invalid", email: "invalid-email" }
```

### Example 5: Filter and Sort with Limit

```yaml
nodes:
  - id: top_products
    type: filter
    config:
      mode: array
      condition:
        field: "stock"
        operator: "gt"
        value: 0
      sort:
        field: "sales"
        order: "desc"
      limit: 5
    inputs:
      data:
        - { id: 1, name: "Product A", stock: 10, sales: 100 }
        - { id: 2, name: "Product B", stock: 5, sales: 500 }
        - { id: 3, name: "Product C", stock: 0, sales: 200 }
```

### Example 6: Object Key Filter

```yaml
nodes:
  - id: filter_object_keys
    type: filter
    config:
      mode: object
      expression: "key.startsWith('user_')"
    inputs:
      data:
        user_name: "Alice"
        user_email: "alice@example.com"
        system_config: "value"
        user_id: "123"
```

Output:
```json
{
  "filtered_data": {
    "user_name": "Alice",
    "user_email": "alice@example.com",
    "user_id": "123"
  },
  "matched_count": 3,
  "total_count": 4,
  "passed": true
}
```

### Example 7: Using Context in Expression

```yaml
nodes:
  - id: set_context
    type: code
    outputs:
      filter_context:
        min_age: 21
        allowed_states: ["CA", "NY", "TX"]

  - id: filter_by_context
    type: filter
    config:
      mode: array
      expression: "item.age >= context.min_age && context.allowed_states.includes(item.state)"
    inputs:
      data:
        - { name: "Alice", age: 25, state: "CA" }
        - { name: "Bob", age: 19, state: "CA" }
        - { name: "Charlie", age: 22, state: "FL" }
      context: $set_context.filter_context
```

### Example 8: Single Mode for Branching

```yaml
nodes:
  - id: check_condition
    type: filter
    config:
      mode: single
      condition:
        field: "status"
        operator: "eq"
        value: "success"
    inputs:
      data:
        status: "success"
        message: "Operation completed"

  - id: branch_on_result
    type: branch
    inputs:
      condition: $check_condition.passed
```

## Expression Syntax

Custom expressions have access to:

| Variable | Description |
|----------|-------------|
| `item` or `$` | Current item being evaluated |
| `context` | Context data from input |
| `_` | Lodash utilities |
| `Math`, `Date`, `String`, `Number`, `Boolean` | Built-in objects |

Example expressions:
```
# Simple comparison
item.price > 100

# Range check
item.age >= 18 && item.age <= 65

# Array includes
item.tags.includes('premium')

# String operations
item.email.endsWith('@company.com')

# Date operations
new Date(item.created) > new Date('2024-01-01')

# Using context
item.department === context.department

# Mathematical
item.score >= context.threshold * 1.5

# Type checking
typeof item.value === 'number'
```

## Best Practices

1. **Use Simple Conditions** for straightforward filtering
2. **Use Expressions** for complex logic or combining multiple field checks
3. **Limit Results** when working with large datasets
4. **Sort After Filtering** for consistent output
5. **Use Context** for external variables and thresholds
6. **Case-Insensitive** for string comparisons when appropriate

## Dependencies

This node requires the following packages:

```bash
npm install lodash@^4.0.0 jsonpath-plus@^7.0.0
```

## License

This custom node is part of AgentGraph and follows the same license.
