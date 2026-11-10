# Data Transform Node

The Data Transform node enables flexible data transformation between JSON, XML, and CSV formats with powerful field mapping capabilities.

## Features

- **Multi-Format Support**: Convert between JSON, XML, and CSV
- **Auto-Detection**: Automatically detect input format when set to "auto"
- **Field Mapping**: Rename and transform fields with mapping rules
- **Data Validation**: Optional schema-based validation
- **Format Customization**: Configurable options for each output format
- **Transform Log**: Detailed statistics about the transformation

## Configuration

### Basic Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `input_format` | string | auto | Input format: json, xml, csv, auto |
| `output_format` | string | required | Output format: json, xml, csv |

### Mapping Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `mapping.enabled` | boolean | false | Enable field mapping |
| `mapping.rules` | array | [] | Array of mapping rules |

Each mapping rule:
| Property | Type | Description |
|----------|------|-------------|
| `source` | string | Source field path (supports dot notation) |
| `target` | string | Target field name |
| `transform` | string | Transformation: none, upper, lower, trim, date, number, boolean |

### JSON Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `json_options.pretty` | boolean | false | Pretty print JSON output |
| `json_options.space` | integer | 2 | Number of spaces for indentation |

### XML Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `xml_options.root_element` | string | root | Root element name |
| `xml_options.item_element` | string | item | Item element name (for arrays) |
| `xml_options.attributes` | boolean | true | Include XML attributes |

### CSV Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `csv_options.delimiter` | string | , | Column delimiter (, ; \t \|) |
| `csv_options.header` | boolean | true | Include header row |
| `csv_options.quote` | string | " | Quote character |
| `csv_options.encoding` | string | utf-8 | Character encoding |

### Validation Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `validation.enabled` | boolean | true | Enable validation |
| `validation.strict` | boolean | false | Fail on validation errors |
| `validation.schema` | object | {} | Field schema (field -> type) |

## Inputs

| Input | Type | Required | Description |
|-------|------|----------|-------------|
| `input_data` | any | true | Input data to transform |
| `mapping_rules` | array | false | Override mapping rules from node config |

## Outputs

| Output | Type | Description |
|--------|------|-------------|
| `output_data` | any | Transformed output data |
| `transform_log` | object | Transformation statistics |

## Usage Examples

### Example 1: JSON to CSV

```yaml
nodes:
  - id: transform_to_csv
    type: data-transform
    config:
      input_format: json
      output_format: csv
      csv_options:
        delimiter: ","
        header: true
    inputs:
      input_data:
        - { name: "Alice", age: 30, city: "NYC" }
        - { name: "Bob", age: 25, city: "LA" }
```

Output:
```csv
"name","age","city"
"Alice",30,"NYC"
"Bob",25,"LA"
```

### Example 2: XML to JSON with Mapping

```yaml
nodes:
  - id: xml_to_json
    type: data-transform
    config:
      input_format: xml
      output_format: json
      json_options:
        pretty: true
      mapping:
        enabled: true
        rules:
          - source: "user.name"
            target: "fullName"
            transform: "upper"
          - source: "user.age"
            target: "userAge"
            transform: "number"
    inputs:
      input_data: |
        <root>
          <user>
            <name>alice smith</name>
            <age>30</age>
          </user>
        </root>
```

Output:
```json
{
  "fullName": "ALICE SMITH",
  "userAge": 30
}
```

### Example 3: CSV to XML

```yaml
nodes:
  - id: csv_to_xml
    type: data-transform
    config:
      input_format: csv
      output_format: xml
      xml_options:
        root_element: "users"
        item_element: "user"
    inputs:
      input_data: |
        name,email,role
        Alice,alice@example.com,admin
        Bob,bob@example.com,user
```

Output:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<root>
  <users>
    <user>
      <name>Alice</name>
      <email>alice@example.com</email>
      <role>admin</role>
    </user>
    <user>
      <name>Bob</name>
      <email>bob@example.com</email>
      <role>user</role>
    </user>
  </users>
</root>
```

### Example 4: Dynamic Mapping from Input

```yaml
nodes:
  - id: prepare_mapping
    type: code
    outputs:
      mapping_rules:
        - source: "firstName"
          target: "first_name"
          transform: "lower"
        - source: "lastName"
          target: "last_name"
          transform: "lower"

  - id: transform_data
    type: data-transform
    inputs:
      input_data:
        - { firstName: "JOHN", lastName: "DOE", age: 30 }
      mapping_rules: $prepare_mapping.mapping_rules
    config:
      input_format: json
      output_format: json
      mapping:
        enabled: true
        rules: []  # Empty, will use input mapping_rules
```

### Example 5: Auto-Detect Format

```yaml
nodes:
  - id: auto_transform
    type: data-transform
    config:
      input_format: auto
      output_format: json
      validation:
        enabled: true
        schema:
          name: string
          email: string
    inputs:
      input_data: '{"name":"Alice","email":"alice@example.com"}'
```

## Transformation Types

The `transform` field in mapping rules supports:

| Transform | Description | Example |
|-----------|-------------|---------|
| `none` | No transformation | `hello` → `hello` |
| `upper` | Uppercase | `hello` → `HELLO` |
| `lower` | Lowercase | `HELLO` → `hello` |
| `trim` | Remove whitespace | `  hello  ` → `hello` |
| `date` | Convert to ISO date | `2024-01-01` → `2024-01-01T00:00:00.000Z` |
| `number` | Convert to number | `"123"` → `123` |
| `boolean` | Convert to boolean | `"true"` → `true` |

## Transform Log

The `transform_log` output provides transformation statistics:

```json
{
  "timestamp": "2024-03-07T12:00:00.000Z",
  "input_format": "json",
  "output_format": "csv",
  "input_size": 1024,
  "output_size": 512,
  "size_ratio": "0.50",
  "mapping_applied": true,
  "validation_applied": true,
  "status": "success"
}
```

## Best Practices

1. **Use Auto-Detection** for flexible input handling
2. **Validate Data** to ensure output quality
3. **Map Fields** to standardize naming conventions
4. **Handle Arrays** - the node automatically handles single objects and arrays
5. **Use Pretty Print** for debugging JSON output

## Dependencies

This node requires the following packages:

```bash
npm install xml2js@^0.6.0 csv-parser@^3.0.0 csv-writer@^1.6.0
```

## License

This custom node is part of AgentGraph and follows the same license.
