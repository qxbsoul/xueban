export interface XuebanOrgDepartmentDraft {
  id: string;
  name: string;
  charter: string;
}

export interface XuebanOrgEmployeeDraft {
  id: string;
  name: string;
  title: string;
  role: string;
  system_prompt: string;
  skills: string[];
  tools: string[];
  department_id: string;
  manager_id: string | null;
  model_name: string;
}

export interface XuebanOrgDraft {
  departments: XuebanOrgDepartmentDraft[];
  employees: XuebanOrgEmployeeDraft[];
}

export class XuebanOrgDraftValidationError extends Error {
  constructor(public readonly issues: string[]) {
    super(issues.join('；'));
    this.name = 'XuebanOrgDraftValidationError';
  }
}

const MAX_DEPARTMENTS = 12;
const MAX_EMPLOYEES = 50;
const MAX_LIST_ITEMS = 30;
const SUPPORTED_MODELS = new Set([
  'claude-sonnet', 'gpt-4o', 'gemini-flash', 'deepseek-flash', 'deepseek-chat', 'qwen-max', 'mimo-v2.6-pro',
]);

function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function requiredString(value: unknown, field: string, issues: string[], max = 500): string {
  if (typeof value !== 'string' || !value.trim()) {
    issues.push(`${field} 必填`);
    return '';
  }
  const text = value.trim();
  if (text.length > max) issues.push(`${field} 不能超过 ${max} 个字符`);
  return text;
}

function stringList(value: unknown, field: string, issues: string[]): string[] {
  if (!Array.isArray(value)) {
    issues.push(`${field} 必须是字符串数组`);
    return [];
  }
  if (value.length > MAX_LIST_ITEMS) issues.push(`${field} 最多 ${MAX_LIST_ITEMS} 项`);
  const result: string[] = [];
  for (const [index, item] of value.entries()) {
    if (typeof item !== 'string' || !item.trim()) {
      issues.push(`${field}[${index}] 必须是非空字符串`);
      continue;
    }
    const text = item.trim();
    if (text.length > 100) issues.push(`${field}[${index}] 不能超过 100 个字符`);
    if (!result.includes(text)) result.push(text);
  }
  return result;
}

/** 只规范化并校验内存草案；此函数没有任何存储或生产写副作用。 */
export function validateXuebanOrgDraft(value: unknown): XuebanOrgDraft {
  const input = object(value);
  const issues: string[] = [];
  if (!Array.isArray(input.departments) || input.departments.length < 1 || input.departments.length > MAX_DEPARTMENTS) {
    issues.push(`部门数量必须为 1–${MAX_DEPARTMENTS}`);
  }
  if (!Array.isArray(input.employees) || input.employees.length < 1 || input.employees.length > MAX_EMPLOYEES) {
    issues.push(`员工数量必须为 1–${MAX_EMPLOYEES}`);
  }

  const departments = (Array.isArray(input.departments) ? input.departments : []).map((item, index) => {
    const dept = object(item);
    return {
      id: requiredString(dept.id, `departments[${index}].id`, issues, 80),
      name: requiredString(dept.name, `departments[${index}].name`, issues, 80),
      charter: requiredString(dept.charter, `departments[${index}].charter`, issues, 2000),
    };
  });
  const employees = (Array.isArray(input.employees) ? input.employees : []).map((item, index) => {
    const employee = object(item);
    const manager = employee.manager_id;
    if (manager !== null && manager !== undefined && (typeof manager !== 'string' || !manager.trim())) {
      issues.push(`employees[${index}].manager_id 必须为草案员工 id 或 null`);
    }
    const modelName = requiredString(employee.model_name, `employees[${index}].model_name`, issues, 80);
    if (modelName && !SUPPORTED_MODELS.has(modelName)) issues.push(`employees[${index}].model_name 不受支持：${modelName}`);
    return {
      id: requiredString(employee.id, `employees[${index}].id`, issues, 80),
      name: requiredString(employee.name, `employees[${index}].name`, issues, 80),
      title: requiredString(employee.title, `employees[${index}].title`, issues, 100),
      role: requiredString(employee.role, `employees[${index}].role`, issues, 100),
      system_prompt: requiredString(employee.system_prompt, `employees[${index}].system_prompt`, issues, 8000),
      skills: stringList(employee.skills, `employees[${index}].skills`, issues),
      tools: stringList(employee.tools, `employees[${index}].tools`, issues),
      department_id: requiredString(employee.department_id, `employees[${index}].department_id`, issues, 80),
      manager_id: typeof manager === 'string' && manager.trim() ? manager.trim() : null,
      model_name: modelName,
    };
  });

  const departmentIds = new Set<string>();
  const departmentNames = new Set<string>();
  for (const dept of departments) {
    if (departmentIds.has(dept.id)) issues.push(`部门 id 重复：${dept.id}`);
    if (departmentNames.has(dept.name.toLocaleLowerCase())) issues.push(`部门名称重复：${dept.name}`);
    departmentIds.add(dept.id);
    departmentNames.add(dept.name.toLocaleLowerCase());
  }
  const employeeIds = new Set<string>();
  const employeeNames = new Set<string>();
  for (const employee of employees) {
    if (employeeIds.has(employee.id)) issues.push(`员工 id 重复：${employee.id}`);
    if (employeeNames.has(employee.name.toLocaleLowerCase())) issues.push(`员工姓名重复：${employee.name}`);
    if (!departmentIds.has(employee.department_id)) issues.push(`员工 ${employee.name || employee.id} 引用了不存在的部门：${employee.department_id}`);
    if (employee.manager_id === employee.id) issues.push(`员工 ${employee.name || employee.id} 不能向自己汇报`);
    employeeIds.add(employee.id);
    employeeNames.add(employee.name.toLocaleLowerCase());
  }
  for (const employee of employees) {
    if (employee.manager_id && !employeeIds.has(employee.manager_id)) {
      issues.push(`员工 ${employee.name || employee.id} 的上级不在草案中：${employee.manager_id}`);
    }
  }

  const managers = new Map(employees.map(employee => [employee.id, employee.manager_id]));
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (id: string): void => {
    if (visited.has(id)) return;
    if (visiting.has(id)) {
      issues.push(`上级关系存在循环：${id}`);
      return;
    }
    visiting.add(id);
    const manager = managers.get(id);
    if (manager && managers.has(manager)) visit(manager);
    visiting.delete(id);
    visited.add(id);
  };
  for (const id of employeeIds) visit(id);

  if (issues.length) throw new XuebanOrgDraftValidationError([...new Set(issues)]);
  return { departments, employees };
}
